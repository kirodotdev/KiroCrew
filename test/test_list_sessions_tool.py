"""Tests for the ``list_sessions`` MCP core tool (browse/overview of the
caller's workspace sessions), including the opt-in ``summarize`` path.

Companion to test_search_chat_history.py — same seeding/dispatch conventions
(KIROCREW_HOME env + mcp_core._call_tool_inner).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

from kiro_crew import mcp_core
from kiro_crew.history import ConversationLog


@pytest.fixture(autouse=True)
def established_session(monkeypatch):
    monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:global-v1")


def _seed_sessions(home):
    """Create a sessions dir with a few transcripts under KIROCREW_HOME=home."""
    sessions = home / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    cl = ConversationLog(base_dir=sessions)
    cl.append("dashboard_chat-1", "user", "how do I configure the redis timeout setting?")
    cl.append("dashboard_chat-1", "assistant", "set redis.timeout in config.json")
    cl.append("dashboard_chat-2", "user", "remind me about the barcelona trip plan")
    # An incognito session — must never surface in the listing.
    cl.append("dashboard_chat-secret", "user", "secret redis password is hunter2")
    cl.update_metadata("dashboard_chat-secret", {"memory_mode": "incognito"})
    return cl


class TestListSessionsTool:
    def test_lists_sessions_with_titles(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        _seed_sessions(tmp_path)
        out = mcp_core._call_tool_inner("list_sessions", {})
        assert "dashboard_chat-1" in out
        assert "dashboard_chat-2" in out
        # First user message becomes the fallback title.
        assert "redis timeout" in out or "barcelona" in out

    def test_incognito_excluded(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        _seed_sessions(tmp_path)
        out = mcp_core._call_tool_inner("list_sessions", {})
        assert "dashboard_chat-secret" not in out
        assert "hunter2" not in out

    def test_empty_returns_message_not_error(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        (tmp_path / "sessions").mkdir(parents=True, exist_ok=True)
        out = mcp_core._call_tool_inner("list_sessions", {})
        assert "No sessions found" in out

    def test_limit_is_respected(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        sessions = tmp_path / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        cl = ConversationLog(base_dir=sessions)
        for i in range(5):
            cl.append(f"dashboard_chat-{i}", "user", f"session number {i}")
        out = mcp_core._call_tool_inner("list_sessions", {"limit": 2})
        # Only two session rows (each row prints one `· \`key\`` line).
        assert out.count("`dashboard_chat-") == 2

    def test_summarize_attaches_summaries(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        _seed_sessions(tmp_path)

        def _fake_post(path, body=None, *, timeout=30, session_key=None):
            assert path == "/api/sessions/summarize"
            assert session_key == "dashboard:global-v1"
            # Return a summary only for chat-1 to prove per-key attach + fallback.
            return {"summaries": {"dashboard_chat-1": "Configuring the redis timeout"}}

        monkeypatch.setattr(mcp_core, "_post", _fake_post)
        out = mcp_core._call_tool_inner("list_sessions", {"summarize": True})
        assert "Configuring the redis timeout" in out
        # chat-2 got no summary → still listed via its title.
        assert "dashboard_chat-2" in out

    def test_summarize_failure_falls_back_to_titles(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        _seed_sessions(tmp_path)

        def _fake_post(path, body=None, *, timeout=30, session_key=None):
            assert session_key == "dashboard:global-v1"
            return {"error": "backend unavailable"}

        monkeypatch.setattr(mcp_core, "_post", _fake_post)
        out = mcp_core._call_tool_inner("list_sessions", {"summarize": True})
        # No crash; the list is still returned with titles.
        assert "dashboard_chat-1" in out
        assert "dashboard_chat-2" in out

    def test_summarize_false_does_not_call_gateway(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        _seed_sessions(tmp_path)

        def _boom(*a, **k):
            raise AssertionError("_post must not be called when summarize is false")

        monkeypatch.setattr(mcp_core, "_post", _boom)
        out = mcp_core._call_tool_inner("list_sessions", {})
        assert "dashboard_chat-1" in out

    def test_workspace_scoping_excludes_other_workspace(self, tmp_path, monkeypatch):
        # list_sessions() rows omit `workspace`, so scoping MUST
        # re-read full metadata. A session tagged to another workspace must not
        # leak to a default-workspace caller.
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        sessions = tmp_path / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        cl = ConversationLog(base_dir=sessions)
        cl.append("dashboard_chat-here", "user", "in my workspace")
        cl.append("dashboard_chat-elsewhere", "user", "belongs to another workspace")
        cl.update_metadata("dashboard_chat-elsewhere", {"workspace": "other-ws"})
        out = mcp_core._call_tool_inner("list_sessions", {})
        assert "dashboard_chat-here" in out
        assert "dashboard_chat-elsewhere" not in out
        # all_workspaces opts out of scoping → the other-workspace session shows.
        out_all = mcp_core._call_tool_inner("list_sessions", {"all_workspaces": True})
        assert "dashboard_chat-elsewhere" in out_all

    def test_row_labels_the_last_active_time_the_sort_uses(self, tmp_path, monkeypatch):
        # The list is ordered by transcript mtime, so each row must show that time,
        # labelled, beside the creation time; a bare creation date reads as a broken sort.
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        sessions = tmp_path / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        cl = ConversationLog(base_dir=sessions)
        cl.append("dashboard_chat-old", "user", "started long ago, still active")
        last_active = datetime(2030, 1, 2, 3, 4, tzinfo=UTC).timestamp()
        os.utime(sessions / "dashboard_chat-old.jsonl", (last_active, last_active))
        out = mcp_core._call_tool_inner("list_sessions", {})
        assert "last active 2030-01-02T03:04 UTC" in out
        assert "created " in out

    def test_row_shows_both_times_in_utc_when_created_carries_an_offset(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        sessions = tmp_path / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        cl = ConversationLog(base_dir=sessions)
        cl.append("dashboard_chat-tokyo", "user", "created on a host nine hours ahead")
        cl.update_metadata("dashboard_chat-tokyo", {"created_at": "2030-01-02T12:04:00+09:00"})
        last_active = datetime(2030, 1, 2, 3, 4, tzinfo=UTC).timestamp()
        os.utime(sessions / "dashboard_chat-tokyo.jsonl", (last_active, last_active))
        out = mcp_core._call_tool_inner("list_sessions", {})
        assert "last active 2030-01-02T03:04 UTC" in out
        assert "created 2030-01-02T03:04 UTC" in out
