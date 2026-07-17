"""Smoke tests for thread-aware context injection.

Tests:
- P0: ChannelHistory strict thread isolation (no cross-thread leakage)
- P1: Trust ACP native history (no thread reminder on follow-ups)
- P2: End-to-end handler wiring
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from kiro_crew.channel_history import ChannelHistory, HistoryEntry

# ── Phase 0: ChannelHistory thread-aware ──


class TestChannelHistoryThreadAware:
    """P0: thread_ts on push/context_for."""

    def test_entry_has_thread_ts(self):
        e = HistoryEntry(user="alice", text="hi", thread_ts="t1")
        assert e.thread_ts == "t1"

    def test_entry_thread_ts_default_none(self):
        e = HistoryEntry(user="alice", text="hi")
        assert e.thread_ts is None

    def test_push_stores_thread_ts(self):
        h = ChannelHistory()
        h.push("C1", "alice", "msg1", thread_ts="t1")
        h.push("C1", "bob", "msg2", thread_ts="t2")
        buf = h._channels["C1"]
        assert buf[0].thread_ts == "t1"
        assert buf[1].thread_ts == "t2"

    def test_push_without_thread_ts(self):
        h = ChannelHistory()
        h.push("C1", "alice", "msg1")
        assert h._channels["C1"][0].thread_ts is None

    def test_context_for_no_thread_ts_original_behavior(self):
        """Without thread_ts, only top-level (non-thread) messages appear."""
        h = ChannelHistory()
        h.push("C1", "alice", "top-level")
        h.push("C1", "bob", "in-thread", thread_ts="t1")
        ctx = h.context_for("C1")
        assert "[Current thread:]" not in ctx
        assert "[Other threads:]" not in ctx
        assert "top-level" in ctx
        assert "in-thread" not in ctx

    def test_context_for_with_thread_ts_splits(self):
        """With thread_ts, only current thread messages appear (strict isolation)."""
        h = ChannelHistory()
        h.push("C1", "alice", "in-thread", thread_ts="t1")
        h.push("C1", "bob", "other-thread", thread_ts="t2")
        h.push("C1", "carol", "also-in-thread", thread_ts="t1")

        ctx = h.context_for("C1", thread_ts="t1")
        assert "[Current thread:]" in ctx
        assert "[Other threads:]" not in ctx
        assert "in-thread" in ctx
        assert "also-in-thread" in ctx
        assert "other-thread" not in ctx

    def test_context_for_thread_ts_no_other_threads(self):
        """All messages in same thread — no [Other threads:] section."""
        h = ChannelHistory()
        h.push("C1", "alice", "msg1", thread_ts="t1")
        h.push("C1", "bob", "msg2", thread_ts="t1")
        ctx = h.context_for("C1", thread_ts="t1")
        assert "[Current thread:]" in ctx
        assert "[Other threads:]" not in ctx

    def test_context_for_thread_ts_no_current_thread(self):
        """No messages match current thread — returns empty."""
        h = ChannelHistory()
        h.push("C1", "alice", "msg1", thread_ts="t2")
        ctx = h.context_for("C1", thread_ts="t1")
        assert ctx == ""

    def test_context_for_empty_channel(self):
        h = ChannelHistory()
        assert h.context_for("C1", thread_ts="t1") == ""

    def test_context_for_thread_ts_none_messages(self):
        """Messages without thread_ts are excluded when filtering by thread."""
        h = ChannelHistory()
        h.push("C1", "alice", "no-thread")  # no thread_ts
        h.push("C1", "bob", "in-thread", thread_ts="t1")
        ctx = h.context_for("C1", thread_ts="t1")
        assert "[Current thread:]" in ctx
        assert "[Other threads:]" not in ctx
        assert "in-thread" in ctx
        assert "no-thread" not in ctx


# ── Phase 1: Trust ACP native history (no thread reminder) ──


class TestNoThreadReminder:
    """P1: follow-up messages trust ACP native history — no reminder injected."""

    def test_no_reminder_on_follow_up(self):
        """Follow-up messages should NOT inject thread reminder — trust ACP."""
        from kiro_crew.context import ContextBuilder
        from kiro_crew.history import ConversationLog

        log = MagicMock(spec=ConversationLog)
        log.recent.return_value = [
            {"role": "user", "content": "what is X?"},
            {"role": "assistant", "content": "X is Y."},
        ]

        cb = ContextBuilder(conversation_log=log)
        msg, _ = cb.build_message(
            "follow up question",
            is_new_session=False,
            session_key="test-thread",
        )
        assert "[Recent thread context" not in msg

    def test_no_reminder_on_new_session(self):
        """New sessions get full history via build_session_context, not reminder."""
        from kiro_crew.context import ContextBuilder
        from kiro_crew.history import ConversationLog

        log = MagicMock(spec=ConversationLog)
        log.recent.return_value = [
            {"role": "user", "content": "hello"},
        ]

        cb = ContextBuilder(conversation_log=log)
        msg, _ = cb.build_message(
            "hi",
            is_new_session=True,
            session_key="test-thread",
        )
        assert "[Recent thread context" not in msg

    def test_no_reminder_without_conversation_log(self):
        from kiro_crew.context import ContextBuilder

        cb = ContextBuilder()
        msg, _ = cb.build_message(
            "hi",
            is_new_session=False,
            session_key="test-thread",
        )
        assert "[Recent thread context" not in msg


# ── Phase 2: Wiring — thread_ts flows through ──


class TestThreadTsWiring:
    """P2: thread_ts passes from handler → build_message → context_for."""

    def test_build_message_passes_thread_ts_to_channel_history(self):
        from kiro_crew.context import ContextBuilder

        mock_ch = MagicMock(spec=ChannelHistory)
        mock_ch.context_for.return_value = "[mocked context]"

        cb = ContextBuilder()
        cb.channel_history = mock_ch

        cb.build_message(
            "test",
            is_new_session=False,
            channel_id="C1",
            thread_ts="t123",
        )
        mock_ch.context_for.assert_called_once_with("C1", thread_ts="t123")

    def test_build_message_no_thread_ts_passes_none(self):
        from kiro_crew.context import ContextBuilder

        mock_ch = MagicMock(spec=ChannelHistory)
        mock_ch.context_for.return_value = ""

        cb = ContextBuilder()
        cb.channel_history = mock_ch

        cb.build_message(
            "test",
            is_new_session=False,
            channel_id="C1",
        )
        mock_ch.context_for.assert_called_once_with("C1", thread_ts=None)


# ── Scenario: The original bug ──


class TestOriginalBugScenario:
    """Reproduce the exact scenario that caused the confusion."""

    def test_two_threads_same_channel_separated(self):
        """Thread A: 'auto skill generation' discussion.
        Thread B: 'kiro sessions' discussion.
        User says '输出断了' in Thread A.
        Agent should only see Thread A messages — strict isolation."""
        h = ChannelHistory()

        # Thread A messages
        h.push("C1", "user", "详细给我解释一下AutoSkill Generation", thread_ts="tA")
        # Thread B message
        h.push("C1", "user", "现在系统里有多少个Kiro sessions", thread_ts="tB")
        # Thread A continuation
        h.push("C1", "user", "输出断了", thread_ts="tA")

        ctx = h.context_for("C1", thread_ts="tA")

        assert "[Current thread:]" in ctx
        assert "[Other threads:]" not in ctx
        assert "AutoSkill Generation" in ctx
        assert "输出断了" in ctx
        assert "Kiro sessions" not in ctx


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
