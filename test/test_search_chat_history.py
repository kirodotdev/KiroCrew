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
        # a dangling "<<<" without its ">>>" (review-bot f-8cbcdff3).
        needle = "q" * 400
        content = "pre " + needle + " post"
        snip = mcp_core._extract_history_snippet([{"role": "user", "content": content}], needle)
        if "<<<" in snip:
            assert ">>>" in snip

    def test_snippet_empty_needle_guarded(self):
        # Empty/whitespace needle must not match-at-0 and wrap garbage.
        assert mcp_core._extract_history_snippet([{"role": "user", "content": "abc"}], "") == ""
        assert mcp_core._extract_history_snippet([{"role": "user", "content": "abc"}], "   ") == ""

    def test_snippet_multi_word_query_falls_back_to_a_token(self):
        # search_sessions matches a session when every TOKEN appears somewhere, so
        # this extractor must locate a token too. Searching only the whole phrase
        # returned "" and the handler suppresses snippet-less rows -- so exactly
        # the multi-word queries token-wise matching enables came back bare.
        msgs = [{"role": "user", "content": "the ack path shows contention under load"}]
        snip = mcp_core._extract_history_snippet(msgs, "ack contention hypotheses")
        assert snip, "a scattered multi-word match must still yield a snippet"
        assert "<<<" in snip and ">>>" in snip, "the located token must be delimited"

    def test_snippet_prefers_the_exact_phrase_over_a_token(self):
        # Phrase first: when the words DO sit together, the snippet centres on the
        # phrase rather than on whichever token happens to appear earliest.
        msgs = [{"role": "user", "content": "ping alone, then the ping pong bench"}]
        snip = mcp_core._extract_history_snippet(msgs, "ping pong")
        assert "<<<ping pong>>>" in snip

    def test_snippet_full_casefold_match_is_delimited(self):
        # The selection (str.casefold().find) and the wrap must use the SAME full
        # casefolding: 'straße'.casefold() == 'strasse' matches 'STRASSE', but a
        # re.IGNORECASE wrap would miss it and return an undelimited snippet.
        msgs = [{"role": "user", "content": "DIE STRASSE IST LANG"}]
        snip = mcp_core._extract_history_snippet(msgs, "straße")
        assert "<<<STRASSE>>>" in snip

    def test_casefold_match_span_maps_source_indices(self):
        # Multi-char fold: the returned span indexes the SOURCE string so the
        # wrap never splits a character.
        span = mcp_core._casefold_match_span("DIE STRASSE IST LANG", "straße".casefold())
        assert span == (4, 11)
        assert mcp_core._casefold_match_span("abc", "zzz") is None

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
        out = mcp_core._call_tool_inner("get_chat_session", {"session_key": "dashboard_chat-1"})
        assert "redis.timeout" in out

    def test_legacy_metadataless_session_still_surfaces(self, tmp_path, monkeypatch):
        # A legacy session file whose first line is a message (predates the
        # metadata line) yields {} from get_metadata. Search must NOT drop it:
        # get_chat_session serves such files fine, so excluding them would hide
        # readable conversations from search.
        import json

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        sessions = tmp_path / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        legacy = sessions / "dashboard_chat-legacy.jsonl"
        legacy.write_text(
            json.dumps({"role": "user", "content": "the legacy widget still works"}) + "\n",
            encoding="utf-8",
        )
        cl = ConversationLog(base_dir=sessions)
        assert cl.get_metadata("dashboard_chat-legacy") == {}  # no metadata line
        out = mcp_core._call_tool_inner("search_chat_history", {"query": "legacy widget"})
        assert "dashboard_chat-legacy" in out

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
        # The not_found early return must never reflect the LLM-supplied key —
        # a crafted credential-bearing key must not appear in the output.
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        _seed_sessions(tmp_path)
        out = mcp_core._call_tool_inner("get_chat_session", {"session_key": "AKIAIOSFODNN7EXAMPLE"})
        assert "AKIAIOSFODNN7EXAMPLE" not in out
        assert "fp:" in out  # not-found now returns a fingerprint, not the raw/echoed key


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


class TestPostMergeHardening:
    """Post-merge security-review hardening regressions."""

    # Redact BEFORE inserting <<<>>> markers so a query that is a substring
    # of a secret in stored content can't split the token and defeat redaction.
    def test_snippet_redacts_credential_even_when_query_is_prefix(self):
        content = "the cred AKIAIOSFODNN7EXAMPLE was rotated"
        snip = mcp_core._extract_history_snippet([{"role": "user", "content": content}], "AKIA")
        assert "AKIAIOSFODNN7EXAMPLE" not in snip
        assert "REDACTED" in snip

    # Casefold length expansion (ß→ss) must not misalign the wrap.
    def test_snippet_casefold_length_expansion_aligned(self):
        content = "die Straße is the street"
        snip = mcp_core._extract_history_snippet([{"role": "user", "content": content}], "straße")
        # The exact matched original text is wrapped, nothing swallowed/shifted.
        assert "<<<Straße>>>" in snip

    # Snippet must come from user/assistant content, not a tool trace.
    def test_snippet_skips_tool_role(self):
        msgs = [
            {"role": "tool", "content": "[trace] redis health probe ok status=200"},
            {"role": "user", "content": "how do I tune the redis timeout?"},
        ]
        snip = mcp_core._extract_history_snippet(msgs, "redis")
        assert "trace" not in snip
        assert "tune the" in snip

    # A non-string workspace value must bucket to "default", not compare
    # unequal and silently hide the session.
    def test_ws_bucket_normalizes_non_string(self):
        assert mcp_core._ws_bucket(["alpha"]) == "default"
        assert mcp_core._ws_bucket(None) == "default"
        assert mcp_core._ws_bucket("") == "default"
        assert mcp_core._ws_bucket("alpha") == "alpha"

    # An impossible calendar date must error, not silently return unfiltered.
    def test_impossible_date_errors(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        _seed_sessions(tmp_path)
        out = mcp_core._call_tool_inner(
            "search_chat_history", {"query": "redis", "before": "2026-02-30"}
        )
        assert "Invalid 'before' date" in out

    # The not-found path must NOT echo the raw key (markdown injection);
    # it returns a fingerprint instead.
    def test_not_found_does_not_reflect_raw_key(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        _seed_sessions(tmp_path)
        payload = "click[here](mailto:attacker-example)"
        out = mcp_core._call_tool_inner("get_chat_session", {"session_key": payload})
        assert payload not in out
        assert "fp:" in out

    # ".." as a substring (not a path component) must NOT be rejected, so
    # search and read agree on which keys are valid.
    def test_dotdot_substring_key_allowed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        cl = ConversationLog(base_dir=tmp_path / "sessions")
        (tmp_path / "sessions").mkdir(parents=True, exist_ok=True)
        cl.append("dashboard_chat-2..3", "user", "redis notes here")
        out = mcp_core._call_tool_inner("get_chat_session", {"session_key": "dashboard_chat-2..3"})
        assert "Invalid session_key" not in out
        assert "redis notes here" in out

    # Many high-score cross-workspace decoys must not starve a real
    # default-bucket caller match ranked below the old limit*3 window.
    def test_no_starvation_from_cross_workspace_decoys(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        sessions = tmp_path / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        cl = ConversationLog(base_dir=sessions)
        # 35 alpha-workspace decoys that score very high on "widget"
        for i in range(35):
            cl.append(f"decoy-{i}", "user", ("widget " * 30))
            cl.update_metadata(f"decoy-{i}", {"workspace": "alpha"})
        # one real default-bucket match
        cl.append("real", "user", "the widget bug we discussed")
        monkeypatch.setattr(mcp_core, "_resolve_session_key", lambda: "")
        out = mcp_core._call_tool_inner("search_chat_history", {"query": "widget", "limit": 5})
        assert "real" in out
        assert "decoy-" not in out
