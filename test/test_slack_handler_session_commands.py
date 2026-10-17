"""Slack handler tests: the owner commands that act on a thread's session (!stop, !title, !compact)."""

from __future__ import annotations

import asyncio

import pytest
from slack_handler_helpers import _clean_approval_state  # noqa: F401
from slack_handler_helpers import FakeProvider, FakeSessionManager

from conftest import MockSlackClient
from kiro_crew.messaging import auto_title
from kiro_crew.providers.base import LLMEvent
from kiro_crew.slack.handler import handle_message, set_allowed_users, set_owner_id

#: These tests exercise the SLACK side (thread renaming), not the record
#: pin, so they pass the value production supplies when there is nothing to
#: pin. ``maybe_auto_title`` requires it, which is what stops a call site
#: from reading the record inside the task and reopening the window.
_PRESENT_PIN = auto_title.RecordPin(auto_title.RECORD_PRESENT, "")


class TestStopCommand:
    """Tests for the !stop kill switch."""

    @pytest.mark.asyncio
    async def test_stop_kills_active_session(self):
        """!stop calls stop_turn and posts confirmation."""
        set_owner_id("U_OWNER")
        set_allowed_users({"U_OWNER"})
        slack = MockSlackClient()
        sessions = FakeSessionManager()
        # Simulate an existing session by marking it as seen
        sessions.keys_seen.append("thread1")
        await handle_message(slack, sessions, "C1", "!stop", "thread1", "msg1", "U_OWNER")
        assert "stop_turn:thread1:force=False" in sessions.removed
        posts = [a for a in slack.actions if a[0] == "post"]
        assert any("Execution stopped" in p[1]["text"] for p in posts)

    @pytest.mark.asyncio
    async def test_stop_no_session_running(self):
        """!stop with no active session replies 'Nothing running.'."""
        set_owner_id("U_OWNER")
        set_allowed_users({"U_OWNER"})
        slack = MockSlackClient()
        sessions = FakeSessionManager()
        # keys_seen is empty — no active session
        await handle_message(slack, sessions, "C1", "!stop", "thread1", "msg1", "U_OWNER")
        posts = [a for a in slack.actions if a[0] == "post"]
        assert any("Nothing running" in p[1]["text"] for p in posts)
        assert "reset:thread1" not in sessions.removed

    @pytest.mark.asyncio
    async def test_stop_denied_for_non_owner(self):
        """!stop is denied for non-owner users (multi-user access disabled)."""
        set_owner_id("U_OWNER")
        set_allowed_users({"U_OWNER", "U_ALLOWED"})  # U_ALLOWED in set but still denied
        slack = MockSlackClient()
        sessions = FakeSessionManager()
        sessions.keys_seen.append("thread1")
        await handle_message(slack, sessions, "C1", "!stop", "thread1", "msg1", "U_ALLOWED")
        assert "reset:thread1" not in sessions.removed
        posts = [a for a in slack.actions if a[0] == "post"]
        assert any(
            "Not authorized" in p[1]["text"]
            or "Owner-only" in p[1]["text"]
            or "authorized" in p[1]["text"].lower()
            for p in posts
        )

    @pytest.mark.asyncio
    async def test_stop_denied_for_unauthorized(self):
        """!stop is denied for users not on the allowlist."""
        set_owner_id("U_OWNER")
        set_allowed_users({"U_OWNER"})
        slack = MockSlackClient()
        sessions = FakeSessionManager()
        sessions.keys_seen.append("thread1")
        await handle_message(slack, sessions, "C1", "!stop", "thread1", "msg1", "U_RANDOM")
        # Session should NOT be stopped
        assert not any("stop_turn:thread1" in r for r in sessions.removed)
        posts = [a for a in slack.actions if a[0] == "post"]
        assert any("Not authorized" in p[1]["text"] for p in posts)

    @pytest.mark.asyncio
    async def test_stop_session_hard_outcome(self):
        """!stop posts hard-kill message when stop_turn returns 'hard'."""
        set_owner_id("U_OWNER")
        set_allowed_users({"U_OWNER"})
        slack = MockSlackClient()
        sessions = FakeSessionManager()
        sessions.keys_seen.append("thread1")
        sessions._stop_outcome = "hard"
        await handle_message(slack, sessions, "C1", "!stop", "thread1", "msg1", "U_OWNER")
        posts = [a for a in slack.actions if a[0] == "post"]
        assert any("session reset" in p[1]["text"] for p in posts)

    @pytest.mark.asyncio
    async def test_slack_stop_posts_ephemeral_stopping_blocks(self):
        """!stop posts an ephemeral message with stopping blocks and Kill Now button."""
        set_owner_id("U_OWNER")
        set_allowed_users({"U_OWNER"})
        slack = MockSlackClient()
        sessions = FakeSessionManager()
        sessions.keys_seen.append("thread1")
        await handle_message(slack, sessions, "C1", "!stop", "thread1", "msg1", "U_OWNER")
        ephemerals = [a for a in slack.actions if a[0] == "ephemeral"]
        assert len(ephemerals) >= 1
        eph = ephemerals[0][1]
        assert eph["channel"] == "C1"
        assert eph["user_id"] == "U_OWNER"
        blocks = eph["blocks"]
        assert any("Stopping" in str(b) for b in blocks)
        # Kill Now button present
        action_blocks = [b for b in blocks if b.get("type") == "actions"]
        assert action_blocks
        elements = action_blocks[0]["elements"]
        assert elements[0]["action_id"] == "stop_kill_now"
        assert elements[0]["value"] == "thread1"

    @pytest.mark.asyncio
    async def test_slack_stop_updates_ephemeral_on_soft_ack(self):
        """On soft ack, on_soft callback posts thread summary."""
        set_owner_id("U_OWNER")
        set_allowed_users({"U_OWNER"})
        slack = MockSlackClient()
        sessions = FakeSessionManager()
        sessions.keys_seen.append("thread1")
        sessions._stop_outcome = "soft"
        await handle_message(slack, sessions, "C1", "!stop", "thread1", "msg1", "U_OWNER")
        posts = [a for a in slack.actions if a[0] == "post"]
        assert any(
            "Execution stopped" in p[1]["text"] and "reset" not in p[1]["text"] for p in posts
        )

    @pytest.mark.asyncio
    async def test_slack_stop_updates_ephemeral_on_hard(self):
        """On hard kill, on_hard callback posts thread summary with reset note."""
        set_owner_id("U_OWNER")
        set_allowed_users({"U_OWNER"})
        slack = MockSlackClient()
        sessions = FakeSessionManager()
        sessions.keys_seen.append("thread1")
        sessions._stop_outcome = "hard"
        await handle_message(slack, sessions, "C1", "!stop", "thread1", "msg1", "U_OWNER")
        posts = [a for a in slack.actions if a[0] == "post"]
        assert any("session reset" in p[1]["text"] for p in posts)

    @pytest.mark.asyncio
    async def test_slack_stop_posts_thread_summary(self):
        """After resolution, a non-ephemeral thread reply is posted."""
        set_owner_id("U_OWNER")
        set_allowed_users({"U_OWNER"})
        slack = MockSlackClient()
        sessions = FakeSessionManager()
        sessions.keys_seen.append("thread1")
        await handle_message(slack, sessions, "C1", "!stop", "thread1", "msg1", "U_OWNER")
        posts = [a for a in slack.actions if a[0] == "post"]
        # At least one non-ephemeral post with stop outcome
        assert any("stopped" in p[1]["text"].lower() for p in posts)

    @pytest.mark.asyncio
    async def test_slack_stop_first_press_clears_queue(self):
        """!stop via stop_turn clears the queue (stop_turn side effect)."""
        set_owner_id("U_OWNER")
        set_allowed_users({"U_OWNER"})
        slack = MockSlackClient()
        sessions = FakeSessionManager()
        sessions.keys_seen.append("thread1")
        await handle_message(slack, sessions, "C1", "!stop", "thread1", "msg1", "U_OWNER")
        # stop_turn was called — it clears queue internally
        assert "stop_turn:thread1:force=False" in sessions.removed


# ── Thread title tests ──────────────────────────────────────────────────


class TestThreadTitle:
    """Tests for !title command and auto-title."""

    @pytest.fixture(autouse=True)
    def _clean_titled_threads(self):
        from kiro_crew.slack.handler import _titled_threads

        _titled_threads.clear()
        yield
        _titled_threads.clear()

    @pytest.mark.asyncio
    async def test_title_sets_thread_title(self):
        """!title <text> calls set_thread_title and reacts."""
        from kiro_crew.slack.handler import _titled_threads

        set_owner_id("U_OWNER")
        set_allowed_users({"U_OWNER"})
        slack = MockSlackClient()
        sessions = FakeSessionManager()
        await handle_message(
            slack, sessions, "C1", "!title ETL Pipeline Debug", "thread1", "msg1", "U_OWNER"
        )
        title_actions = [a for a in slack.actions if a[0] == "set_thread_title"]
        assert len(title_actions) == 1
        assert title_actions[0][1]["title"] == "ETL Pipeline Debug"
        assert title_actions[0][1]["thread_ts"] == "thread1"
        assert "thread1" in _titled_threads

    @pytest.mark.asyncio
    async def test_title_no_args_shows_usage(self):
        """!title with no text shows usage message."""
        set_owner_id("U_OWNER")
        set_allowed_users({"U_OWNER"})
        slack = MockSlackClient()
        sessions = FakeSessionManager()
        await handle_message(slack, sessions, "C1", "!title", "thread1", "msg1", "U_OWNER")
        posts = [a for a in slack.actions if a[0] == "post"]
        assert any("Usage" in p[1]["text"] for p in posts)

    @pytest.mark.asyncio
    async def test_title_denied_for_non_owner(self):
        """!title is denied for non-owner users (multi-user access disabled)."""
        set_owner_id("U_OWNER")
        set_allowed_users({"U_OWNER", "U_ALLOWED"})  # U_ALLOWED in set but still denied
        slack = MockSlackClient()
        sessions = FakeSessionManager()
        await handle_message(
            slack, sessions, "C1", "!title My Thread", "thread2", "msg2", "U_ALLOWED"
        )
        title_actions = [a for a in slack.actions if a[0] == "set_thread_title"]
        assert len(title_actions) == 0

    @pytest.mark.asyncio
    async def test_title_denied_for_unauthorized(self):
        """!title is denied for users not on the allowlist."""
        set_owner_id("U_OWNER")
        set_allowed_users({"U_OWNER"})
        slack = MockSlackClient()
        sessions = FakeSessionManager()
        await handle_message(slack, sessions, "C1", "!title Sneaky", "thread1", "msg1", "U_RANDOM")
        title_actions = [a for a in slack.actions if a[0] == "set_thread_title"]
        assert len(title_actions) == 0
        posts = [a for a in slack.actions if a[0] == "post"]
        assert any("Not authorized" in p[1]["text"] for p in posts)

    @pytest.mark.asyncio
    async def test_title_truncated_to_80_chars(self):
        """!title truncates to 80 characters."""
        set_owner_id("U_OWNER")
        set_allowed_users({"U_OWNER"})
        slack = MockSlackClient()
        sessions = FakeSessionManager()
        long_title = "A" * 120
        await handle_message(
            slack, sessions, "C1", f"!title {long_title}", "thread1", "msg1", "U_OWNER"
        )
        title_actions = [a for a in slack.actions if a[0] == "set_thread_title"]
        assert len(title_actions) == 1
        assert len(title_actions[0][1]["title"]) == 80


class TestAutoTitleSlack:
    """Tests for _maybe_auto_title_slack — background auto-titling."""

    @pytest.fixture(autouse=True)
    def _clean_titled_threads(self):
        # The claim LRU and its lock live in `messaging.auto_title`; `reset()` does
        # both halves, which matters because a test that crashed mid-title leaves
        # the claim marked AND the permit held.
        from kiro_crew.messaging import auto_title

        auto_title.reset()
        yield
        auto_title.reset()

    @pytest.mark.asyncio
    async def test_auto_title_happy_path(self):
        """Valid LLM title → set_thread_title called, session_key stays in _titled_threads."""
        from kiro_crew.slack.handler import _mark_titled, _maybe_auto_title_slack, _titled_threads

        slack = MockSlackClient()
        sessions = FakeSessionManager()
        sessions._provider = FakeProvider([LLMEvent(kind="text_chunk", text="ETL Debug Session")])
        _mark_titled("sk1")
        await _maybe_auto_title_slack(
            slack,
            sessions,
            "C1",
            "sk1",
            None,
            "help me",
            "sure",
            pin=_PRESENT_PIN,
        )
        title_actions = [a for a in slack.actions if a[0] == "set_thread_title"]
        assert len(title_actions) == 1
        assert title_actions[0][1]["title"] == "ETL Debug Session"
        assert "sk1" in _titled_threads

    @pytest.mark.asyncio
    async def test_auto_title_unexpected_error_surfaces_at_warning(self, caplog):
        """An unexpected failure is logged at WARNING with the exception type.

        This blanket handler runs on a fire-and-forget task, so its log line is
        the only place a real defect surfaces. At DEBUG it masks a deterministic
        cross-loop ``RuntimeError`` into an order-dependent CI flake. The claim
        must also be released so the next exchange retries.
        """
        import logging

        from kiro_crew.slack.handler import _mark_titled, _maybe_auto_title_slack, _titled_threads

        class ExplodingSessionManager(FakeSessionManager):
            async def get_or_create(self, key, agent=None, channel_id=None, start_priority=None):
                raise RuntimeError("bound to a different event loop")

        slack = MockSlackClient()
        _mark_titled("sk-err")
        # The shared module's logger: the blanket handler this pins lives in
        # `messaging.auto_title` now, and naming the wrong logger is not a harmless
        # miss — `at_level` also SETS the level, so a DEBUG assertion against a
        # logger that emits nothing passes vacuously (its sibling below did).
        with caplog.at_level(logging.WARNING, logger="kiro_crew.messaging.auto_title"):
            await _maybe_auto_title_slack(
                slack,
                ExplodingSessionManager(),
                "C1",
                "sk-err",
                None,
                "help",
                "sure",
                pin=_PRESENT_PIN,
            )

        warnings = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "auto-title failed" in r.getMessage()
        ]
        assert len(warnings) == 1, "unexpected error must surface at WARNING, not debug"
        assert "RuntimeError" in warnings[0].getMessage()
        assert warnings[0].exc_info is not None, "traceback must be attached"
        # Claim released so the next exchange can retry.
        assert "sk-err" not in _titled_threads
        # And nothing was titled.
        assert not [a for a in slack.actions if a[0] == "set_thread_title"]

    @pytest.mark.asyncio
    async def test_auto_title_stream_timeout_stays_at_debug(self, caplog):
        """A title-stream timeout is routine noise, not the masking concern:
        it must NOT emit the WARNING traceback (one per exchange on a slow
        model would be log spam), while still releasing the claim for retry."""
        import logging

        from kiro_crew.slack.handler import _mark_titled, _maybe_auto_title_slack, _titled_threads

        class TimingOutSessionManager(FakeSessionManager):
            async def get_or_create(self, key, agent=None, channel_id=None, start_priority=None):
                raise asyncio.TimeoutError()

        slack = MockSlackClient()
        _mark_titled("sk-slow")
        with caplog.at_level(logging.DEBUG, logger="kiro_crew.messaging.auto_title"):
            await _maybe_auto_title_slack(
                slack,
                TimingOutSessionManager(),
                "C1",
                "sk-slow",
                None,
                "help",
                "sure",
                pin=_PRESENT_PIN,
            )

        assert not [
            r for r in caplog.records if r.levelno >= logging.WARNING
        ], "timeout must not warn"
        assert [
            r
            for r in caplog.records
            if r.levelno == logging.DEBUG and "timed out" in r.getMessage()
        ]
        assert "sk-slow" not in _titled_threads  # claim released for retry

    @pytest.mark.asyncio
    async def test_auto_title_skip_removes_claim(self):
        """LLM returns SKIP → no title set, session_key removed from _titled_threads."""
        from kiro_crew.slack.handler import _mark_titled, _maybe_auto_title_slack, _titled_threads

        slack = MockSlackClient()
        sessions = FakeSessionManager()
        sessions._provider = FakeProvider([LLMEvent(kind="text_chunk", text="SKIP")])
        _mark_titled("sk2")
        await _maybe_auto_title_slack(
            slack,
            sessions,
            "C1",
            "sk2",
            None,
            "hi",
            "hello",
            pin=_PRESENT_PIN,
        )
        title_actions = [a for a in slack.actions if a[0] == "set_thread_title"]
        assert len(title_actions) == 0
        assert "sk2" not in _titled_threads

    @pytest.mark.asyncio
    async def test_auto_title_error_removes_claim(self):
        """Exception during streaming → session_key removed from _titled_threads for retry."""
        from kiro_crew.slack.handler import _mark_titled, _maybe_auto_title_slack, _titled_threads

        slack = MockSlackClient()
        sessions = FakeSessionManager()
        sessions._provider = None  # will cause AttributeError
        _mark_titled("sk3")
        await _maybe_auto_title_slack(
            slack,
            sessions,
            "C1",
            "sk3",
            None,
            "test",
            "test",
            pin=_PRESENT_PIN,
        )
        assert "sk3" not in _titled_threads

    @pytest.mark.asyncio
    async def test_auto_title_with_curly_braces(self):
        """User text with curly braces doesn't crash or skip title."""
        from kiro_crew.slack.handler import _mark_titled, _maybe_auto_title_slack

        slack = MockSlackClient()
        sessions = FakeSessionManager()
        sessions._provider = FakeProvider([LLMEvent(kind="text_chunk", text="JSON Debug Session")])
        _mark_titled("sk4")
        await _maybe_auto_title_slack(
            slack,
            sessions,
            "C1",
            "sk4",
            None,
            'parse this: {"key": "value"}',
            "sure, here's the parsed output",
            pin=_PRESENT_PIN,
        )
        title_actions = [a for a in slack.actions if a[0] == "set_thread_title"]
        assert len(title_actions) == 1
        assert title_actions[0][1]["title"] == "JSON Debug Session"

    @pytest.mark.asyncio
    async def test_title_updates_conversation_log(self):
        """!title persists to conversation_log when available."""
        from unittest.mock import MagicMock

        from kiro_crew.slack.handler import _handle_slash_command

        mock_log = MagicMock()
        slack = MockSlackClient()
        sessions = FakeSessionManager()
        await _handle_slash_command(
            "!title ETL Debug",
            slack,
            sessions,
            "C1",
            "thread1",
            "msg1",
            "thread1",
            "U_OWNER",
            conversation_log=mock_log,
        )
        mock_log.set_title.assert_called_once_with("thread1", "ETL Debug")


class TestCompactCommand:
    """Tests for the compact / !compact keyword command."""

    @pytest.fixture(autouse=True)
    def _setup_owner(self):
        set_owner_id("U_OWNER")
        set_allowed_users({"U_OWNER"})

    def _make_provider_with_compact(self, events=None):
        """Create a FakeProvider whose /compact runs via the prompt transport
        (provider.compact() + wait_for_compaction()), the path #276 fixed.

        The optional ``events`` list (compaction_status LLMEvents) is
        translated into the terminal result wait_for_compaction() returns, so
        existing call sites keep expressing the outcome the same way.
        """
        provider = FakeProvider(events)
        result = {"type": "completed", "summary": "Summary preserved"}
        if events is not None:
            result = {"type": "timeout", "summary": ""}
            for e in events:
                if e.kind == "compaction_status" and e.text in ("completed", "failed"):
                    result = {"type": e.text, "summary": e.title or ""}

        async def compact(context=""):
            return None

        async def wait_for_compaction(timeout=120.0):
            return result

        provider.compact = compact
        provider.wait_for_compaction = wait_for_compaction
        return provider

    def _make_sessions_with_active(self, provider):
        """Create a FakeSessionManager with an active session accessible via get_provider()."""
        sessions = FakeSessionManager(provider)
        sessions.keys_seen.append("thread1")

        class _FakeSession:
            def __init__(self, p):
                self.provider = p

        sessions._sessions = {"thread1": _FakeSession(provider)}
        return sessions

    def _posted_texts(self, slack):
        """Extract text from all post_message actions."""
        return [a[1]["text"] for a in slack.actions if a[0] == "post"]

    @pytest.mark.asyncio
    async def test_compact_keyword_triggers_compaction(self):
        provider = self._make_provider_with_compact()
        sessions = self._make_sessions_with_active(provider)
        slack = MockSlackClient()

        await handle_message(slack, sessions, "C1", "!compact", "thread1", "msg1", "U_OWNER")

        texts = self._posted_texts(slack)
        assert any("Compacting" in t for t in texts)
        assert any("✅" in t for t in texts)

    @pytest.mark.asyncio
    async def test_bare_compact_does_not_trigger(self):
        """Bare 'compact' without ! prefix is not a command."""
        provider = self._make_provider_with_compact()
        sessions = self._make_sessions_with_active(provider)
        slack = MockSlackClient()

        await handle_message(slack, sessions, "C1", "compact", "thread1", "msg1", "U_OWNER")

        texts = self._posted_texts(slack)
        # The compact command posts "🔄 Compacting context…" — bare word must not.
        assert not any("Compacting context" in t for t in texts)

    @pytest.mark.asyncio
    async def test_compact_no_session_replies_no_session(self):
        sessions = FakeSessionManager()
        slack = MockSlackClient()

        await handle_message(slack, sessions, "C1", "!compact", "thread1", "msg1", "U_OWNER")

        texts = self._posted_texts(slack)
        assert any("No active session" in t for t in texts)

    @pytest.mark.asyncio
    async def test_compact_declined_on_auto_managed_backend(self):
        # A backend that cannot serve /compact (the provider names it via
        # manual_compact_unsupported_backend) gets the informational reply and
        # compact() is NEVER dispatched.
        provider = self._make_provider_with_compact()
        calls = []

        async def _compact(context=""):
            calls.append(1)

        provider.compact = _compact
        provider.manual_compact_unsupported_backend = "kas"
        sessions = self._make_sessions_with_active(provider)
        slack = MockSlackClient()

        await handle_message(slack, sessions, "C1", "!compact", "thread1", "msg1", "U_OWNER")

        texts = self._posted_texts(slack)
        assert any("manages compaction automatically" in t for t in texts)
        assert not any("Compacting context" in t for t in texts)
        assert calls == []

    @pytest.mark.asyncio
    async def test_compact_none_capability_preserves_dispatch(self):
        # The ABC's None (supported) default keeps the existing dispatch.
        provider = self._make_provider_with_compact()
        provider.manual_compact_unsupported_backend = None
        sessions = self._make_sessions_with_active(provider)
        slack = MockSlackClient()

        await handle_message(slack, sessions, "C1", "!compact", "thread1", "msg1", "U_OWNER")

        texts = self._posted_texts(slack)
        assert any("✅" in t for t in texts)

    @pytest.mark.asyncio
    async def test_compact_failed_reports_error(self):
        provider = self._make_provider_with_compact(
            [
                LLMEvent(kind="compaction_status", text="failed", title="out of memory"),
            ]
        )
        sessions = self._make_sessions_with_active(provider)
        slack = MockSlackClient()

        await handle_message(slack, sessions, "C1", "!compact", "thread1", "msg1", "U_OWNER")

        texts = self._posted_texts(slack)
        assert any("❌" in t and "out of memory" in t for t in texts)

    @pytest.mark.asyncio
    async def test_compact_with_summary_keeps_internal_body_private(self):
        provider = self._make_provider_with_compact(
            [
                LLMEvent(
                    kind="compaction_status",
                    text="completed",
                    title="## OBJECTIVE\nKept 5 key topics",
                ),
            ]
        )
        sessions = self._make_sessions_with_active(provider)
        slack = MockSlackClient()

        await handle_message(slack, sessions, "C1", "!compact", "thread1", "msg1", "U_OWNER")

        texts = self._posted_texts(slack)
        visible = " ".join(texts)
        assert "Context compacted" in visible
        assert "OBJECTIVE" not in visible and "Kept 5 key topics" not in visible

    @pytest.mark.asyncio
    async def test_compact_does_not_create_session(self):
        """compact should not fall through to LLM session creation."""
        sessions = FakeSessionManager()
        slack = MockSlackClient()

        await handle_message(slack, sessions, "C1", "!compact", "thread1", "msg1", "U_OWNER")

        # get_or_create should NOT have been called
        assert len(sessions.keys_seen) == 0

    @pytest.mark.asyncio
    async def test_compact_unauthorized_user_is_blocked(self):
        """compact from unauthorized user is denied with a message."""
        provider = self._make_provider_with_compact()
        sessions = self._make_sessions_with_active(provider)
        slack = MockSlackClient()

        await handle_message(slack, sessions, "C1", "!compact", "thread1", "msg1", "U_RANDOM")

        texts = self._posted_texts(slack)
        # Should NOT have triggered compaction
        assert not any("Compacting" in t for t in texts)
        # Should have posted a denial message
        assert any("Not authorized" in t for t in texts)
        # Should NOT have fallen through to LLM session creation
        assert len(sessions.keys_seen) == 1, "Message must not create a new LLM session"

    @pytest.mark.asyncio
    async def test_compact_keeps_session_alive(self):
        """Session stays alive after compact — kiro-cli does not kill the process."""
        provider = self._make_provider_with_compact()
        sessions = self._make_sessions_with_active(provider)
        slack = MockSlackClient()

        await handle_message(slack, sessions, "C1", "!compact", "thread1", "msg1", "U_OWNER")

        assert "thread1" not in sessions.removed, "Session must NOT be removed after compact"

    @pytest.mark.asyncio
    async def test_compact_deferred_via_wait_for_compaction(self):
        """The terminal status arrives via wait_for_compaction (async after end_turn)."""
        provider = self._make_provider_with_compact(events=[])  # no mid-turn status

        async def wait_for_compaction(timeout=120.0):
            return {"type": "completed", "summary": "Deferred summary"}

        provider.wait_for_compaction = wait_for_compaction
        sessions = self._make_sessions_with_active(provider)
        slack = MockSlackClient()

        await handle_message(slack, sessions, "C1", "!compact", "thread1", "msg1", "U_OWNER")

        texts = self._posted_texts(slack)
        visible = " ".join(texts)
        assert "Context compacted" in visible
        assert "Deferred summary" not in visible

    @pytest.mark.asyncio
    async def test_compact_exception_cleans_up(self):
        """When compact() raises, handler posts an error and tears the session down.

        The teardown discards the wedged native conversation rather than
        destroying the session-map entry, so the thread keeps its binding.
        """
        provider = FakeProvider()

        async def compact(context=""):
            raise RuntimeError("process died")

        provider.compact = compact
        sessions = self._make_sessions_with_active(provider)
        slack = MockSlackClient()

        await handle_message(slack, sessions, "C1", "!compact", "thread1", "msg1", "U_OWNER")

        texts = self._posted_texts(slack)
        assert any("unexpectedly" in t for t in texts)
        assert (
            "discard:thread1" in sessions.removed
        ), "Session must be torn down after compact failure"
        assert "destroy:thread1" not in sessions.removed, "the thread binding must survive"

    @pytest.mark.asyncio
    async def test_compact_posts_timing_footer_without_ctx(self):
        """Compact footer omits ctx% — cached value is stale post-compaction."""
        provider = self._make_provider_with_compact()
        sessions = self._make_sessions_with_active(provider)
        slack = MockSlackClient()

        await handle_message(slack, sessions, "C1", "!compact", "thread1", "msg1", "U_OWNER")

        blocks_calls = [a for a in slack.actions if a[0] == "blocks"]
        assert blocks_calls, "Expected a post_blocks call for the timing footer"
        footer = blocks_calls[-1][1]
        assert footer["blocks"][0]["type"] == "context"
        assert "Finished in" in footer["text"]
        assert "ctx" not in footer["text"], "Footer must NOT include stale ctx% after compact"

    @pytest.mark.asyncio
    async def test_compact_timeout_reports_gracefully(self):
        """A compaction that yields no terminal status reports a graceful
        timeout and keeps the session (regression: nested 120s timeouts made
        this branch dead code and destroyed a healthy session)."""
        provider = self._make_provider_with_compact()

        async def wait_for_compaction(timeout=120.0):
            return {"type": "timeout"}

        provider.wait_for_compaction = wait_for_compaction
        sessions = self._make_sessions_with_active(provider)
        slack = MockSlackClient()

        await handle_message(slack, sessions, "C1", "!compact", "thread1", "msg1", "U_OWNER")

        texts = self._posted_texts(slack)
        assert any("timed out" in t for t in texts)
        assert "destroy:thread1" not in sessions.removed

    @pytest.mark.asyncio
    async def test_compact_busy_refuses_without_starting(self):
        """While a turn holds the session semaphore, /compact refuses politely
        and never starts a second prompt or tears the session down."""
        provider = self._make_provider_with_compact()
        sessions = self._make_sessions_with_active(provider)

        async def try_acquire(key):
            return False  # a turn is already in flight

        sessions.try_acquire = try_acquire
        slack = MockSlackClient()

        await handle_message(slack, sessions, "C1", "!compact", "thread1", "msg1", "U_OWNER")

        texts = self._posted_texts(slack)
        assert any("Still working" in t for t in texts)
        assert not any("Compacting context" in t for t in texts)  # never started
        assert "destroy:thread1" not in sessions.removed
