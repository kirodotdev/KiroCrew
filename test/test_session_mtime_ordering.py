"""Regression tests for session-ordering stability.

Root cause (session picker landing on a stale/closed thread after a gateway
restart): ``ConversationLog.list_sessions`` orders sessions by file mtime as a
proxy for "last activity", but housekeeping rewrites (consolidation, rotation,
metadata/tab_id backfill on restart) were bumping that mtime to "now". On every
restart the lifecycle consolidates + rehydrates open slots, so long-closed
sessions leapt to the top of the list and ``kirocrew token`` / a new Slack
session resolved to a stale thread.

Fix: only a genuine ``append`` advances a session's mtime; housekeeping
rewrites restore the pre-write mtime. Plus ``DashboardState.resolve_slot``
breaks ``chat-N`` prefix ties by newest timestamp, not dict-iteration order.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

from kiro_crew.dashboard.state import DashboardState
from kiro_crew.history import ConversationLog


class TestHousekeepingPreservesMtime:
    def _aged(self, path, mtime: float) -> None:
        os.utime(path, (mtime, mtime))

    def test_update_metadata_preserves_mtime(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        log.append("s1", "user", "hi")
        p = tmp_path / "s1.jsonl"
        self._aged(p, 1_000_000.0)
        log.update_metadata("s1", {"title": "renamed", "tab_id": "abc123"})
        assert abs(p.stat().st_mtime - 1_000_000.0) < 1.0

    def test_mark_consolidated_preserves_mtime(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        log.append("s1", "user", "hi")
        log.append("s1", "assistant", "yo")
        p = tmp_path / "s1.jsonl"
        self._aged(p, 1_000_000.0)
        log.mark_consolidated("s1", 1)
        assert abs(p.stat().st_mtime - 1_000_000.0) < 1.0

    def test_rewrite_session_preserves_mtime(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        for i in range(6):
            log.append("s1", "user", f"m{i}")
        p = tmp_path / "s1.jsonl"
        self._aged(p, 1_000_000.0)
        # Compact down to the last 2 messages (housekeeping).
        keep = log._read_messages("s1")[-2:]
        log.rewrite_session("s1", keep)
        assert abs(p.stat().st_mtime - 1_000_000.0) < 1.0
        # Content was really rewritten (proves mtime restore isn't a no-op).
        assert len(log._read_messages("s1")) == 2

    def test_append_still_advances_mtime(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        log.append("s1", "user", "hi")
        p = tmp_path / "s1.jsonl"
        self._aged(p, 1_000_000.0)
        log.append("s1", "assistant", "genuine new activity")
        # A real message must move the session to "now", not stay in the past.
        assert p.stat().st_mtime > 1_000_000.0 + 60

    def test_update_metadata_upsert_new_file_gets_natural_mtime(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        # No prior file → upsert path → should NOT be forced to epoch 0.
        log.update_metadata("fresh", {"agent": "kirocrew"})
        p = tmp_path / "fresh.jsonl"
        assert p.exists()
        assert p.stat().st_mtime > 1_700_000_000  # a real, recent timestamp


class TestListSessionsOrderingStable:
    def test_housekeeping_does_not_float_stale_session(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        log.append("old", "user", "old msg")
        log.append("new", "user", "new msg")
        old_p, new_p = tmp_path / "old.jsonl", tmp_path / "new.jsonl"
        os.utime(old_p, (1_000.0, 1_000.0))
        os.utime(new_p, (2_000.0, 2_000.0))

        keys = [s["key"] for s in log.list_sessions()]
        assert keys.index("new") < keys.index("old")

        # Simulate the restart lifecycle touching the OLD session (consolidation
        # + tab_id backfill). Pre-fix this bumped old's mtime to "now" and
        # floated it above "new".
        log.mark_consolidated("old", 1)
        log.update_metadata("old", {"tab_id": "xyz"})

        keys = [s["key"] for s in log.list_sessions()]
        assert keys.index("new") < keys.index("old")


class TestResolveSlotTieBreak:
    def _state(self, tmp_path):
        sessions = MagicMock(count=0)
        sessions.get_pid = MagicMock(return_value=None)
        sessions.remove = AsyncMock()
        sessions.get_slack_link = MagicMock(return_value=(None, None))
        return DashboardState(
            sessions=sessions,
            crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
            lessons=MagicMock(load_all=MagicMock(return_value=[])),
            start_time=0.0,
            conversation_log=ConversationLog(base_dir=tmp_path),
        )

    def test_prefers_newest_timestamp_slot(self, tmp_path):
        state = self._state(tmp_path)
        # Insert the STALE slot first so a dict-iteration tie-break would (wrongly)
        # return it; the fix must return the newest-timestamp slot instead.
        state.get_or_create_slot("chat-3-100")
        newest = state.get_or_create_slot("chat-3-900")
        resolved = state.resolve_slot("chat-3")
        assert resolved is not None
        assert resolved.key == newest.key == "chat-3-900"

    def test_exact_match_wins(self, tmp_path):
        state = self._state(tmp_path)
        exact = state.get_or_create_slot("chat-3-100")
        assert state.resolve_slot("chat-3-100").key == exact.key

    def test_no_match_returns_none(self, tmp_path):
        state = self._state(tmp_path)
        state.get_or_create_slot("chat-3-100")
        assert state.resolve_slot("chat-9") is None
