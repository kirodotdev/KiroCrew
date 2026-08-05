"""Tests for fresh-slot first-turn duplicate user-message injection.

Race condition: dashboard chat handler appends the user message to slot.messages
and the periodic flush loop (5 s) writes it to JSONL. Meanwhile _run_chat awaits
get_or_create which spawns kiro-cli (~15 s cold). When kiro is finally up, the
context-builder reads the JSONL — and finds the user's CURRENT message there,
which it then injects as "history" alongside the same message under
[CURRENT USER REQUEST].

The fix: ConversationLog.recent / recent_chained / recent_with_provenance now
accept ``exclude_last_n`` to drop trailing raw entries before role filtering.
The dashboard caller (chat_runner._run_chat) passes exclude_last_n=1 because
exactly one message is appended per turn before _run_chat invokes context
build.
"""
from __future__ import annotations

from kiro_crew.context import build_session_replay
from kiro_crew.history import ConversationLog


class TestRecentExcludeLastN:
    def test_recent_default_returns_everything(self, tmp_path):
        """exclude_last_n defaults to 0 — backward-compatible."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "u1")
        log.append("k", "assistant", "a1")
        log.append("k", "user", "u2")

        result = log.recent("k", roles={"user", "assistant"})

        assert [m["content"] for m in result] == ["u1", "a1", "u2"]

    def test_recent_exclude_last_n_drops_trailing_user(self, tmp_path):
        """exclude_last_n=1 drops the just-flushed current-turn user message."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "u1")
        log.append("k", "assistant", "a1")
        log.append("k", "user", "u_current")  # the racing flush

        result = log.recent("k", roles={"user", "assistant"}, exclude_last_n=1)

        assert [m["content"] for m in result] == ["u1", "a1"]

    def test_recent_exclude_applies_before_role_filter(self, tmp_path):
        """exclude_last_n drops raw entries BEFORE role filter, so a trailing
        inject/subagent entry doesn't cause a legitimate user/assistant entry
        to be dropped instead.
        """
        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "u1")
        log.append("k", "assistant", "a1")
        log.append("k", "subagent", "[Subagent completion event] …")

        result = log.recent("k", roles={"user", "assistant"}, exclude_last_n=1)

        assert [m["content"] for m in result] == ["u1", "a1"]

    def test_recent_exclude_zero_disk_no_crash(self, tmp_path):
        """exclude_last_n on an empty log returns []."""
        log = ConversationLog(base_dir=tmp_path)

        result = log.recent("k", roles={"user", "assistant"}, exclude_last_n=1)

        assert result == []

    def test_recent_exclude_larger_than_disk(self, tmp_path):
        """If exclude_last_n exceeds total entries, slice returns []."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "u1")

        result = log.recent("k", roles={"user", "assistant"}, exclude_last_n=5)

        assert result == []

    def test_recent_with_provenance_exclude_last_n(self, tmp_path):
        """recent_with_provenance also honors exclude_last_n."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "u1", source_thread="dashboard")
        log.append("k", "assistant", "a1", source_thread="dashboard")
        log.append("k", "user", "u_current", source_thread="dashboard")

        result = log.recent_with_provenance("k", exclude_last_n=1)

        assert [r["snippet"] for r in result] == ["u1", "a1"]

    def test_recent_chained_exclude_last_n(self, tmp_path):
        """recent_chained falls back to single-file read for sessions without
        tab_id, so exclude_last_n behaves identically to recent() in that case.
        """
        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "u1")
        log.append("k", "assistant", "a1")
        log.append("k", "user", "u_current")

        result = log.recent_chained("k", roles={"user", "assistant"}, exclude_last_n=1)

        assert [m["content"] for m in result] == ["u1", "a1"]


class TestBuildSessionReplayMesh1726:
    def test_build_session_replay_drops_current_turn(self, tmp_path):
        """End-to-end: build_session_replay with exclude_last_n=1 returns None
        when the only on-disk message is the just-flushed current-turn user msg.
        """
        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "what time is it?")

        replay = build_session_replay(log, "k", exclude_last_n=1)

        assert replay is None

    def test_build_session_replay_keeps_real_history(self, tmp_path):
        """Sanity: with prior turns on disk, exclude_last_n=1 keeps them."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "earlier question")
        log.append("k", "assistant", "earlier answer")
        log.append("k", "user", "current question")  # the race-flushed msg

        replay = build_session_replay(log, "k", exclude_last_n=1)

        assert replay is not None
        assert "earlier question" in replay
        assert "earlier answer" in replay
        assert "current question" not in replay

    def test_build_session_replay_default_behavior_unchanged(self, tmp_path):
        """Backward compat: exclude_last_n=0 (default) preserves existing behavior."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "msg1")
        log.append("k", "assistant", "resp1")

        replay = build_session_replay(log, "k")

        assert replay is not None
        assert "msg1" in replay
        assert "resp1" in replay
