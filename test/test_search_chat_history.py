"""Tests for the Phase 1 chat-history search tools (search_chat_history /
get_chat_session) and their helpers in mcp_core.

These exercise the acceptance criteria EB-1, EB-3, EB-4, EB-5, EB-7b from
~/.kirocrew/workspace/design-docs/search-chat-history-design.md.
"""

from __future__ import annotations

from kiro_crew import mcp_core
from kiro_crew.history import ConversationLog

# ── Pure helpers ──


class TestHelpers:
    def test_snippet_delimits_match(self):
        msgs = [{"role": "user", "content": "we deployed redis to the staging cluster today"}]
        snip = mcp_core._extract_history_snippet(msgs, "redis")
        assert "<<<redis>>>" in snip

    def test_snippet_empty_when_only_title_matched(self):
        msgs = [{"role": "user", "content": "totally unrelated body text"}]
        assert mcp_core._extract_history_snippet(msgs, "barcelona") == ""

    def test_snippet_is_bounded(self):
        long = "x" * 5000 + " needle " + "y" * 5000
        snip = mcp_core._extract_history_snippet([{"role": "user", "content": long}], "needle")
        assert len(snip) <= mcp_core._SNIPPET_MAX_LEN

    def test_snippet_truncation_never_leaves_open_delimiter(self):
        # A long query whose match + delimiters exceed the cap must not produce
        # a dangling "<<<" without its ">>>" (AutoSDE f-8cbcdff3).
        needle = "q" * 400
        content = "pre " + needle + " post"
        snip = mcp_core._extract_history_snippet([{"role": "user", "content": content}], needle)
        if "<<<" in snip:
            assert ">>>" in snip

    def test_snippet_empty_needle_guarded(self):
        # Empty/whitespace needle must not match-at-0 and wrap garbage.
        assert mcp_core._extract_history_snippet([{"role": "user", "content": "abc"}], "") == ""
        assert mcp_core._extract_history_snippet([{"role": "user", "content": "abc"}], "   ") == ""

    def test_incognito_detection(self):
        assert mcp_core._history_is_incognito({"memory_mode": "incognito"})
        assert mcp_core._history_is_incognito({"memory_mode": "temporary"})
        assert not mcp_core._history_is_incognito({"memory_mode": "persistent"})
        assert not mcp_core._history_is_incognito({})

    def test_parse_iso_date_epoch(self):
        assert mcp_core._parse_iso_date_epoch("2026-01-01") is not None
        assert mcp_core._parse_iso_date_epoch("not-a-date") is None


# ── Handler integration (env-driven config home) ──


def _seed_sessions(home):
    """Create a sessions dir with a few transcripts under KIROCREW_HOME=home."""
    sessions = home / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    cl = ConversationLog(base_dir=sessions)
    cl.append("dashboard_chat-1", "user", "how do I configure the redis timeout setting?")
    cl.append("dashboard_chat-1", "assistant", "set redis.timeout in config.json")
    cl.append("dashboard_chat-2", "user", "remind me about the barcelona trip plan")
    # An incognito session that also matches "redis" — must never surface.
    cl.append("dashboard_chat-secret", "user", "secret redis password is hunter2")
    cl.update_metadata("dashboard_chat-secret", {"memory_mode": "incognito"})
    return cl


class TestSearchChatHistoryHandler:
    def test_basic_match_and_snippet(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        _seed_sessions(tmp_path)
        out = mcp_core._call_tool_inner("search_chat_history", {"query": "redis"})
        assert "dashboard_chat-1" in out  # EB-1
        assert "<<<redis>>>" in out or "redis" in out  # EB-3

    def test_no_match_returns_message_not_error(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        _seed_sessions(tmp_path)
        out = mcp_core._call_tool_inner("search_chat_history", {"query": "zzzznomatch"})
        assert "No matching conversations" in out  # EB-4

    def test_incognito_excluded(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        _seed_sessions(tmp_path)
        out = mcp_core._call_tool_inner("search_chat_history", {"query": "redis"})
        assert "dashboard_chat-secret" not in out  # EB-5
        assert "hunter2" not in out

    def test_snippet_redacts_credential(self, tmp_path, monkeypatch):
        # EB-6: the standard dual-redaction floor runs on tool output, so a
        # credential pattern (e.g. an AWS access key) in a matched message is
        # redacted in the returned snippet. (This is the same redaction every
        # external surface applies — not a stronger, URL-stripping guarantee.)
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        sessions = tmp_path / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        cl = ConversationLog(base_dir=sessions)
        cl.append(
            "dashboard_chat-leak",
            "user",
            "the widget deploy used key AKIAIOSFODNN7EXAMPLE today",
        )
        out = mcp_core._call_tool_inner("search_chat_history", {"query": "widget"})
        assert "AKIAIOSFODNN7EXAMPLE" not in out  # redacted
        assert "REDACTED" in out

    def test_get_chat_session_returns_transcript(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        _seed_sessions(tmp_path)
        out = mcp_core._call_tool_inner(
            "get_chat_session", {"session_key": "dashboard_chat-1"}
        )
        assert "redis.timeout" in out

    def test_get_chat_session_refuses_incognito(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        _seed_sessions(tmp_path)
        out = mcp_core._call_tool_inner(
            "get_chat_session", {"session_key": "dashboard_chat-secret"}
        )
        assert "private" in out.lower()  # EB-7b
        assert "hunter2" not in out


class TestDateFilter:
    def test_after_filter_excludes_old_sessions(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        _seed_sessions(tmp_path)
        # A future 'after' date should drop today's freshly-written sessions.
        out = mcp_core._call_tool_inner(
            "search_chat_history", {"query": "redis", "after": "2099-01-01"}
        )
        assert "No matching conversations" in out  # EB-7

    def test_before_filter_excludes_recent_sessions(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        _seed_sessions(tmp_path)
        # A past 'before' date should drop today's sessions (modified now).
        out = mcp_core._call_tool_inner(
            "search_chat_history", {"query": "redis", "before": "2000-01-01"}
        )
        assert "No matching conversations" in out  # EB-7

    def test_wide_window_includes_match(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        _seed_sessions(tmp_path)
        out = mcp_core._call_tool_inner(
            "search_chat_history",
            {"query": "redis", "after": "2000-01-01", "before": "2099-01-01"},
        )
        assert "dashboard_chat-1" in out


class TestWorkspaceScope:
    def _seed_two_workspaces(self, home):
        sessions = home / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        cl = ConversationLog(base_dir=sessions)
        # Caller's own session, workspace = "alpha"
        cl.append("dashboard_chat-self", "user", "kickoff in workspace alpha")
        cl.update_metadata("dashboard_chat-self", {"workspace": "alpha"})
        # A matching session in the SAME workspace
        cl.append("dashboard_chat-alpha", "user", "the widget bug in alpha")
        cl.update_metadata("dashboard_chat-alpha", {"workspace": "alpha"})
        # A matching session in a DIFFERENT workspace
        cl.append("dashboard_chat-beta", "user", "the widget bug in beta")
        cl.update_metadata("dashboard_chat-beta", {"workspace": "beta"})
        return cl

    def test_scoped_to_current_workspace_by_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        self._seed_two_workspaces(tmp_path)
        # Resolve caller identity to the alpha-workspace session.
        monkeypatch.setattr(mcp_core, "_resolve_session_key", lambda: "dashboard_chat-self")
        out = mcp_core._call_tool_inner("search_chat_history", {"query": "widget bug"})
        assert "dashboard_chat-alpha" in out  # EB-cc3: same workspace surfaces
        assert "dashboard_chat-beta" not in out  # other workspace hidden

    def test_all_workspaces_opt_in(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        self._seed_two_workspaces(tmp_path)
        monkeypatch.setattr(mcp_core, "_resolve_session_key", lambda: "dashboard_chat-self")
        out = mcp_core._call_tool_inner(
            "search_chat_history", {"query": "widget bug", "all_workspaces": True}
        )
        assert "dashboard_chat-alpha" in out
        assert "dashboard_chat-beta" in out  # opt-in surfaces both

    def test_unresolvable_caller_scopes_to_default_not_all(self, tmp_path, monkeypatch):
        # Fail-closed: an unresolvable caller (no workspace) must NOT fail open to
        # every workspace. It scopes to the "default" bucket (unset workspace).
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        self._seed_two_workspaces(tmp_path)
        # Add an unset-workspace ("default" bucket) match.
        cl = ConversationLog(base_dir=tmp_path / "sessions")
        cl.append("dashboard_chat-default", "user", "the widget bug in default ws")
        monkeypatch.setattr(mcp_core, "_resolve_session_key", lambda: "")
        out = mcp_core._call_tool_inner("search_chat_history", {"query": "widget bug"})
        assert "dashboard_chat-default" in out  # default bucket included
        assert "dashboard_chat-alpha" not in out  # named workspaces excluded
        assert "dashboard_chat-beta" not in out


class TestSessionKeySafety:
    def test_get_chat_session_rejects_traversal_key(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        _seed_sessions(tmp_path)
        for bad in ("../../etc/passwd", "x/..\\y", "a/../b"):
            out = mcp_core._call_tool_inner("get_chat_session", {"session_key": bad})
            assert "Invalid session_key" in out

    def test_get_chat_session_redacts_key_on_not_found(self, tmp_path, monkeypatch):
        # AutoSDE security-controls: the not_found early return echoes the
        # LLM-supplied key — it MUST pass through dual redaction so a crafted
        # credential-bearing key isn't reflected unredacted.
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        _seed_sessions(tmp_path)
        out = mcp_core._call_tool_inner(
            "get_chat_session", {"session_key": "AKIAIOSFODNN7EXAMPLE"}
        )
        assert "AKIAIOSFODNN7EXAMPLE" not in out
        assert "REDACTED" in out


class TestGetChatSessionWorkspaceGate:
    def _seed(self, home):
        sessions = home / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        cl = ConversationLog(base_dir=sessions)
        cl.append("dashboard_chat-self", "user", "alpha caller")
        cl.update_metadata("dashboard_chat-self", {"workspace": "alpha"})
        cl.append("dashboard_chat-alpha", "user", "secret alpha content")
        cl.update_metadata("dashboard_chat-alpha", {"workspace": "alpha"})
        cl.append("dashboard_chat-beta", "user", "secret beta content")
        cl.update_metadata("dashboard_chat-beta", {"workspace": "beta"})
        return cl

    def test_same_workspace_allowed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        self._seed(tmp_path)
        monkeypatch.setattr(mcp_core, "_resolve_session_key", lambda: "dashboard_chat-self")
        out = mcp_core._call_tool_inner("get_chat_session", {"session_key": "dashboard_chat-alpha"})
        assert "secret alpha content" in out

    def test_cross_workspace_denied(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        self._seed(tmp_path)
        monkeypatch.setattr(mcp_core, "_resolve_session_key", lambda: "dashboard_chat-self")
        out = mcp_core._call_tool_inner("get_chat_session", {"session_key": "dashboard_chat-beta"})
        assert "Access denied" in out
        assert "secret beta content" not in out

    def test_cross_workspace_all_workspaces_opt_in(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        self._seed(tmp_path)
        monkeypatch.setattr(mcp_core, "_resolve_session_key", lambda: "dashboard_chat-self")
        out = mcp_core._call_tool_inner(
            "get_chat_session", {"session_key": "dashboard_chat-beta", "all_workspaces": True}
        )
        assert "secret beta content" in out
