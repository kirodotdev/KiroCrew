"""Tests for conversation history module."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.history import (
    _CONSOLIDATION_THRESHOLD,
    _SESSION_KEEP_LINES,
    ConversationLog,
    HistoryConsolidator,
)


class TestConversationLog:
    def test_append_creates_file(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        log.append("thread1", "user", "hello")
        path = tmp_path / "thread1.jsonl"
        assert path.exists()
        lines = path.read_text().splitlines()
        assert len(lines) == 2  # metadata + message
        meta = json.loads(lines[0])
        assert meta["_type"] == "metadata"
        msg = json.loads(lines[1])
        assert msg["role"] == "user"
        assert msg["content"] == "hello"

    def test_append_multiple(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        log.append("t1", "user", "hi")
        log.append("t1", "assistant", "hello!")
        log.append("t1", "user", "how are you?")
        messages = log._read_messages("t1")
        assert len(messages) == 3

    def test_recent(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        for i in range(25):
            log.append("t1", "user", f"msg {i}")
        recent = log.recent("t1", max_messages=5)
        assert len(recent) == 5
        assert recent[0]["content"] == "msg 20"
        assert recent[4]["content"] == "msg 24"

    def test_recent_empty_session(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        assert log.recent("nonexistent") == []

    def test_provenance(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        log.append("t1", "user", "hello", source_thread="1234.5678", source_user="U123")
        log.append("t1", "assistant", "hi there")
        prov = log.recent_with_provenance("t1")
        assert len(prov) == 1
        assert prov[0]["source_thread"] == "1234.5678"
        assert "hello" in prov[0]["snippet"]

    def test_provenance_empty(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        log.append("t1", "user", "hello")  # no provenance
        assert log.recent_with_provenance("t1") == []

    def test_unconsolidated_count(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        for i in range(10):
            log.append("t1", "user", f"msg {i}")
        assert log.unconsolidated_count("t1") == 10

    def test_mark_consolidated(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        for i in range(10):
            log.append("t1", "user", f"msg {i}")
        log.mark_consolidated("t1", 7)
        assert log.unconsolidated_count("t1") == 3
        unconsolidated, total = log.get_unconsolidated("t1")
        assert len(unconsolidated) == 3
        assert total == 10

    def test_mark_consolidated_nonexistent(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        log.mark_consolidated("nonexistent", 5)  # should not raise

    def test_load_transcript(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        log.append("t1", "user", "what is 2+2?")
        log.append("t1", "assistant", "4")
        transcript = log.load_transcript("t1")
        assert "User: what is 2+2?" in transcript
        assert "Assistant: 4" in transcript

    def test_load_transcript_empty(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        assert log.load_transcript("nonexistent") == ""

    def test_safe_key_sanitizes(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        log.append("thread:with/special chars!", "user", "hi")
        # Should create a file with sanitized name
        files = list(tmp_path.glob("*.jsonl"))
        assert len(files) == 1
        assert "/" not in files[0].name
        assert ":" not in files[0].name

    def test_tools_saved(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        log.append("t1", "assistant", "done", tools=["ReadFile", "WriteFile"])
        messages = log._read_messages("t1")
        assert messages[0]["tools"] == ["ReadFile", "WriteFile"]

    def test_rotation(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        # Need > 200 lines AND > 2MB to trigger rotation
        content = "x" * 10000
        for i in range(300):
            log.append("t1", "user", f"{content} msg {i}")
        path = tmp_path / "t1.jsonl"
        lines = path.read_text().splitlines()
        # Should have metadata + kept lines (+ a few from post-rotation appends)
        assert len(lines) <= _SESSION_KEEP_LINES + 5

    def test_rotation_resets_consolidated(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        # Need > 200 lines AND > 2MB to trigger rotation
        content = "x" * 10000
        for i in range(250):
            log.append("t1", "user", f"{content} msg {i}")
        log.mark_consolidated("t1", 200)
        # Add more to trigger rotation again
        for i in range(100):
            log.append("t1", "user", f"{content} more {i}")
        # After rotation, last_consolidated should be reset to 0
        meta = log._read_metadata("t1")
        assert meta.get("last_consolidated") == 0

    def test_corrupted_json_lines_skipped(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        log.append("t1", "user", "good message")
        # Inject corrupted line
        path = tmp_path / "t1.jsonl"
        with open(path, "a") as f:
            f.write("this is not json\n")
        log.append("t1", "user", "another good message")
        messages = log._read_messages("t1")
        assert len(messages) == 2  # corrupted line skipped

    def test_metadata_missing(self, tmp_path):
        """Session file without metadata line should still work."""
        log = ConversationLog(base_dir=tmp_path)
        path = tmp_path / "t1.jsonl"
        # Write messages without metadata
        path.write_text(json.dumps({"role": "user", "content": "hi", "ts": "2026-01-01"}) + "\n")
        messages = log._read_messages("t1")
        assert len(messages) == 1
        assert log.unconsolidated_count("t1") == 1  # offset defaults to 0

    def test_init_creates_dir(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        log = ConversationLog(base_dir=sessions_dir)
        log.init()
        assert sessions_dir.is_dir()

    def test_append_persists_agent_in_metadata(self, tmp_path):
        """Initial metadata line should carry the agent when provided."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("t1", "user", "hello", agent="kiro-v2")
        meta = log.get_metadata("t1")
        assert meta.get("agent") == "kiro-v2"

    def test_append_without_agent_omits_field(self, tmp_path):
        """Omitting agent leaves the field absent, not an empty string."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("t1", "user", "hello")
        meta = log.get_metadata("t1")
        assert "agent" not in meta

    def test_append_agent_only_set_on_file_create(self, tmp_path):
        """Subsequent appends with a different agent do NOT overwrite metadata.

        Changing the agent mid-session must go through update_metadata(),
        not another append() call.  This keeps append() cheap (no read).
        """
        log = ConversationLog(base_dir=tmp_path)
        log.append("t1", "user", "first", agent="alpha")
        log.append("t1", "user", "second", agent="beta")
        meta = log.get_metadata("t1")
        assert meta.get("agent") == "alpha"

    def test_update_metadata_changes_agent(self, tmp_path):
        """update_metadata() must be able to mutate the agent post-creation."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("t1", "user", "hello", agent="alpha")
        log.update_metadata("t1", {"agent": "beta"})
        meta = log.get_metadata("t1")
        assert meta.get("agent") == "beta"

    def test_update_metadata_upserts_when_file_absent(self, tmp_path):
        """update_metadata() on a not-yet-created session must create the file.

        Regression: ``!ta <agent> --clean`` issued before the first message is
        logged used to be silently dropped (the file did not exist yet), so the
        agent/clean_mode selection lived only in memory and was lost on restart
        -- the session then resumed under the default agent with full tools.
        """
        log = ConversationLog(base_dir=tmp_path)
        log.update_metadata("fresh", {"agent": "artemis", "clean_mode": True})
        meta = log.get_metadata("fresh")
        assert meta.get("agent") == "artemis"
        assert meta.get("clean_mode") is True
        assert meta.get("_type") == "metadata"
        # A subsequent append must NOT clobber the upserted metadata line.
        log.append("fresh", "user", "hello")
        meta2 = log.get_metadata("fresh")
        assert meta2.get("agent") == "artemis"
        assert meta2.get("clean_mode") is True

    def test_list_sessions_surfaces_agent(self, tmp_path):
        """list_sessions() should include the agent field when present."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("t-with", "user", "hi", agent="kiro-v2")
        log.append("t-without", "user", "hi")
        by_key = {s["key"]: s for s in log.list_sessions()}
        assert by_key["t-with"].get("agent") == "kiro-v2"
        assert "agent" not in by_key["t-without"]

    def test_list_sessions_surfaces_folder_id(self, tmp_path):
        """list_sessions() should surface folder_id from the metadata line when present."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("t-filed", "user", "hi")
        log.update_metadata("t-filed", {"folder_id": "folder-123"})
        log.append("t-unfiled", "user", "hi")
        by_key = {s["key"]: s for s in log.list_sessions()}
        assert by_key["t-filed"].get("folder_id") == "folder-123"
        assert "folder_id" not in by_key["t-unfiled"]

    def test_search_sessions_surfaces_folder_id(self, tmp_path):
        """search_sessions() results carry folder_id — the frontend groups on it."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("t-kms", "user", "investigate the kms rollback")
        log.update_metadata("t-kms", {"folder_id": "folder-cpb"})
        results = {s["key"]: s for s in log.search_sessions("kms")}
        assert "t-kms" in results
        assert results["t-kms"].get("folder_id") == "folder-cpb"


class TestRewriteSession:
    def test_rewrite_replaces_content(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        for i in range(20):
            log.append("t1", "user", f"msg {i}")
        log.rewrite_session("t1", [{"role": "user", "content": "recent", "ts": "now"}])
        messages = log._read_messages("t1")
        assert len(messages) == 1
        assert messages[0]["content"] == "recent"

    def test_rewrite_sets_compacted_metadata(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        log.append("t1", "user", "hello")
        log.rewrite_session("t1", [{"role": "user", "content": "kept", "ts": "now"}])
        meta = log._read_metadata("t1")
        assert "compacted_at" in meta
        assert meta["last_consolidated"] == 0

    def test_rewrite_creates_dir_if_missing(self, tmp_path):
        sessions_dir = tmp_path / "new_sessions"
        log = ConversationLog(base_dir=sessions_dir)
        log.rewrite_session("t1", [{"role": "user", "content": "hi", "ts": "now"}])
        assert (sessions_dir / "t1.jsonl").exists()

    def test_rewrite_empty_messages(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        log.append("t1", "user", "hello")
        log.rewrite_session("t1", [])
        messages = log._read_messages("t1")
        assert messages == []
        # Metadata should still exist
        meta = log._read_metadata("t1")
        assert meta["_type"] == "metadata"

    def test_rewrite_atomic(self, tmp_path):
        """Rewrite uses tmp file — original should not be corrupted on crash."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("t1", "user", "original")
        # Verify no .tmp file left behind after successful rewrite
        log.rewrite_session("t1", [{"role": "user", "content": "new", "ts": "now"}])
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert tmp_files == []


class TestRecentFromSource:
    def test_recent_from_source_collects_across_sessions(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        log.append("dashboard:chat-1-100", "user", "hello from chat 1")
        log.append("dashboard:chat-1-100", "assistant", "hi back from 1")
        log.append("dashboard:chat-2-200", "user", "hello from chat 2")
        log.append("dashboard:chat-2-200", "assistant", "hi back from 2")
        result = log.recent_from_source("dashboard:")
        assert len(result) == 4
        contents = [m["content"] for m in result]
        assert "hello from chat 1" in contents
        assert "hello from chat 2" in contents

    def test_recent_from_source_excludes_key(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        log.append("dashboard:chat-1-100", "user", "hello 1")
        log.append("dashboard:chat-2-200", "user", "hello 2")
        result = log.recent_from_source("dashboard:", exclude_key="dashboard:chat-1-100")
        assert len(result) == 1
        assert result[0]["content"] == "hello 2"

    def test_recent_from_source_respects_max(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        for i in range(30):
            log.append("dashboard:chat-1-100", "user", f"msg {i}")
        result = log.recent_from_source("dashboard:", max_messages=5)
        assert len(result) == 5
        assert result[-1]["content"] == "msg 29"

    def test_recent_from_source_no_match(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        log.append("slack:thread-123", "user", "hello from slack")
        result = log.recent_from_source("dashboard:")
        assert result == []

    def test_recent_from_source_empty_dir(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path / "nonexistent")
        result = log.recent_from_source("dashboard:")
        assert result == []

    def test_recent_from_source_sorted_by_ts(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        # Append in different sessions — timestamps will be ordered
        log.append("dashboard:chat-1-100", "user", "first")
        log.append("dashboard:chat-2-200", "user", "second")
        log.append("dashboard:chat-1-100", "user", "third")
        result = log.recent_from_source("dashboard:")
        contents = [m["content"] for m in result]
        assert contents == ["first", "second", "third"]


class TestSessionManagerCompaction:
    def test_sliding_window_splits_messages(self, tmp_path):
        from kiro_crew.history import ConversationLog

        log = ConversationLog(base_dir=tmp_path)
        log.init()
        # 10 messages = 5 pairs
        for i in range(10):
            role = "user" if i % 2 == 0 else "assistant"
            log.append("t1", role, f"msg-{i}")

        older, recent = log.sliding_window("t1", keep_recent=2)
        # keep 2 pairs = 4 messages recent, 6 older
        assert len(older) == 6
        assert len(recent) == 4
        assert recent[0]["content"] == "msg-6"

    def test_sliding_window_all_recent_when_few(self, tmp_path):
        from kiro_crew.history import ConversationLog

        log = ConversationLog(base_dir=tmp_path)
        log.init()
        log.append("t1", "user", "hello")
        log.append("t1", "assistant", "hi")

        older, recent = log.sliding_window("t1", keep_recent=5)
        assert len(older) == 0
        assert len(recent) == 2


class TestCanonicalKey:
    """Tests for ConversationLog._canonical_key — stacked dashboard_ prefix collapse."""

    def test_non_dashboard_key_unchanged(self):
        assert ConversationLog._canonical_key("slack-thread-123") == "slack-thread-123"

    def test_single_prefix_unchanged(self):
        assert ConversationLog._canonical_key("dashboard_chat-1-100") == "dashboard_chat-1-100"

    def test_double_prefix_collapsed(self):
        assert ConversationLog._canonical_key("dashboard_dashboard_chat-1-100") == "dashboard_chat-1-100"

    def test_triple_prefix_collapsed(self):
        assert ConversationLog._canonical_key("dashboard_dashboard_dashboard_x") == "dashboard_x"

    def test_empty_string(self):
        assert ConversationLog._canonical_key("") == ""

    def test_dashboard_only_returns_self(self):
        # "dashboard_" with nothing after stripping → returns original
        assert ConversationLog._canonical_key("dashboard_") == "dashboard_"


class TestHasLog:
    def test_exists(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        log.append("thread-1", "user", "hello")
        assert log.has_log("thread-1") is True

    def test_not_exists(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        assert log.has_log("nonexistent") is False


class TestListSessionsDedup:
    """Tests for list_sessions symlink skip and stacked-prefix deduplication."""

    def test_skips_symlinks(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        log.append("original-session", "user", "hello")
        # Create a symlink alias
        src = tmp_path / "original-session.jsonl"
        dst = tmp_path / "alias-session.jsonl"
        dst.symlink_to(src.name)
        sessions = log.list_sessions()
        keys = [s["key"] for s in sessions]
        assert "original-session" in keys
        assert "alias-session" not in keys

    def test_deduplicates_stacked_prefixes(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        # Create two files that are canonical duplicates
        log.append("dashboard_chat-1-100", "user", "original")
        log.append("dashboard_dashboard_chat-1-100", "user", "duplicate")
        sessions = log.list_sessions()
        # Should only have one entry for this canonical key
        canon_keys = [ConversationLog._canonical_key(s["key"]) for s in sessions]
        assert canon_keys.count("dashboard_chat-1-100") == 1

    def test_dedup_keeps_newer(self, tmp_path):
        import os

        log = ConversationLog(base_dir=tmp_path)
        log.append("dashboard_chat-1-100", "user", "older")
        log.append("dashboard_dashboard_chat-1-100", "user", "newer")
        # Make the double-prefix file newer
        older = tmp_path / "dashboard_chat-1-100.jsonl"
        os.utime(older, (1000, 1000))
        sessions = log.list_sessions()
        keys = [s["key"] for s in sessions]
        assert "dashboard_dashboard_chat-1-100" in keys
        assert "dashboard_chat-1-100" not in keys

    def test_sorted_by_modified_not_created(self, tmp_path):
        """Regression: sessions must sort by modified time, not created time.

        An older session that was recently updated should appear before a
        newer session that hasn't been touched.  Sorting by 'created' would
        put the newer-but-stale session first — that's the bug we're guarding
        against (see commit 789209e, reverted by f04690d, re-fixed in 07a7099).
        """
        import os

        log = ConversationLog(base_dir=tmp_path)

        log.append("session-a", "user", "old session")
        log.append("session-b", "user", "new session")

        # Force deterministic mtimes: B older, A newer
        os.utime(tmp_path / "session-b.jsonl", (1000, 1000))
        os.utime(tmp_path / "session-a.jsonl", (2000, 2000))

        sessions = log.list_sessions()
        keys = [s["key"] for s in sessions]
        assert keys[0] == "session-a", (
            "Sessions must be sorted by modified time — "
            "session-a was touched most recently and should be first"
        )


class TestAgentUsage:
    """Tests for ConversationLog.agent_usage() session-frequency aggregation."""

    def test_counts_sessions_per_agent(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        log.append("s1", "user", "hi", agent="beta")
        log.append("s2", "user", "hi", agent="beta")
        log.append("s3", "user", "hi", agent="alpha")

        usage = log.agent_usage()

        assert usage["beta"][0] == 2
        assert usage["alpha"][0] == 1

    def test_last_used_is_max_mtime(self, tmp_path):
        import os

        log = ConversationLog(base_dir=tmp_path)
        log.append("s_old", "user", "hi", agent="beta")
        log.append("s_new", "user", "hi", agent="beta")
        os.utime(tmp_path / "s_old.jsonl", (1000, 1000))
        os.utime(tmp_path / "s_new.jsonl", (5000, 5000))

        usage = log.agent_usage()

        assert usage["beta"][0] == 2
        assert usage["beta"][1] == 5000.0

    def test_ignores_agentless_sessions(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        log.append("s1", "user", "hi", agent="alpha")
        log.append("s2", "user", "hi")  # no agent recorded

        usage = log.agent_usage()

        assert "alpha" in usage
        assert len(usage) == 1

    def test_empty_when_no_sessions(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)

        assert log.agent_usage() == {}

    def test_inherits_symlink_skip_and_dedup(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        # Symlink alias should not double-count the agent.
        log.append("original", "user", "hi", agent="alpha")
        (tmp_path / "alias.jsonl").symlink_to("original.jsonl")
        # Stacked-prefix canonical duplicate should not double-count either.
        log.append("dashboard_chat-1-100", "user", "hi", agent="alpha")
        log.append("dashboard_dashboard_chat-1-100", "user", "hi", agent="alpha")

        usage = log.agent_usage()

        # 1 for "original" + 1 for the deduped stacked-prefix pair = 2.
        assert usage["alpha"][0] == 2


class TestSearchSessions:
    """Tests for content search over session JSONL files."""

    def test_matches_content(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        log.append("alpha", "user", "discussed CR-268092218 today")
        log.append("beta", "user", "unrelated chat")
        results = log.search_sessions("CR-268092218")
        keys = [s["key"] for s in results]
        assert keys == ["alpha"]

    def test_case_insensitive(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        log.append("alpha", "user", "KMS access denied exception")
        results = log.search_sessions("kms ACCESS")
        assert [s["key"] for s in results] == ["alpha"]

    def test_empty_query_returns_empty(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        log.append("alpha", "user", "anything")
        assert log.search_sessions("") == []

    def test_respects_limit(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        for i in range(5):
            log.append(f"s{i}", "user", "match")
        results = log.search_sessions("match", limit=2)
        assert len(results) == 2

    def test_no_match_returns_empty(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        log.append("alpha", "user", "hello world")
        assert log.search_sessions("zzznope") == []

    def test_ignores_json_structural_fields(self, tmp_path):
        """Query must match message ``content`` only, not JSON keys/values.

        Regression: searching for common tokens like ``user`` or ``role``
        used to hit every file because the raw JSONL contains
        ``"role": "user"`` on every line.
        """
        log = ConversationLog(base_dir=tmp_path)
        log.append("alpha", "user", "hello there")
        # "role" appears in every JSONL line as a structural key — must not match
        assert log.search_sessions("role") == []
        # "user" appears as the role value — must not match on that alone
        assert log.search_sessions("user") == []
        # But a real content substring does match
        assert [s["key"] for s in log.search_sessions("hello")] == ["alpha"]

    def test_matches_query_with_json_escaped_chars(self, tmp_path):
        """Query containing backslash/quote must match despite JSON escaping.

        Regression: file paths like ``src\\kiro_crew`` are stored in JSONL
        as ``src\\\\kiro_crew`` (escaped).  A raw-line substring fast-path
        would miss them; parsing every line ensures the needle is compared
        against the un-escaped ``content`` value.
        """
        log = ConversationLog(base_dir=tmp_path)
        log.append("alpha", "user", r"edited src\kiro_crew\history.py today")
        results = log.search_sessions(r"src\kiro_crew")
        assert [s["key"] for s in results] == ["alpha"]

    def test_case_insensitive_unicode(self, tmp_path):
        """Non-ASCII case folding — ``Über`` in the file must match ``über``.

        NOTE: this test writes the JSONL file directly instead of using
        :meth:`ConversationLog.append` because Python's ``json.dumps``
        defaults to ``ensure_ascii=True`` and would escape ``Ü`` as
        ``\\u00dc``.  That would bypass the Unicode case-folding code path
        we want to exercise.  Writing raw UTF-8 simulates future storage
        formats or externally-pasted content that may contain non-ASCII
        bytes verbatim.
        """
        (tmp_path / "alpha.jsonl").write_text(
            '{"key": "alpha", "title": "alpha", "created": "2025-01-01T00:00:00"}\n'
            '{"role": "user", "content": "Über alles"}\n',
            encoding="utf-8",
        )
        log = ConversationLog(base_dir=tmp_path)
        results = log.search_sessions("über")
        assert [s["key"] for s in results] == ["alpha"]

    def test_casefold_matches_sharp_s(self, tmp_path):
        """``str.casefold`` folds German ``ß`` to ``ss`` so ``strasse`` matches ``straße``.

        ``str.lower`` (previous impl) left ``ß`` unchanged, so a search
        for ``strasse`` would miss content containing ``straße``.
        """
        (tmp_path / "alpha.jsonl").write_text(
            '{"key": "alpha", "title": "alpha", "created": "2025-01-01T00:00:00"}\n'
            '{"role": "user", "content": "Hauptstraße 5"}\n',
            encoding="utf-8",
        )
        log = ConversationLog(base_dir=tmp_path)
        assert [s["key"] for s in log.search_sessions("hauptstrasse")] == ["alpha"]

    def test_title_match_ranks_above_content_only(self, tmp_path):
        """Title match gets field boost, outranking a content-only match."""
        # content-only match, newer (would win on recency alone)
        (tmp_path / "content-only.jsonl").write_text(
            '{"_type": "metadata", "title": "unrelated topic"}\n'
            '{"role": "user", "content": "we discussed apollo deploy"}\n',
            encoding="utf-8",
        )
        # title match, older
        (tmp_path / "title-match.jsonl").write_text(
            '{"_type": "metadata", "title": "apollo troubleshooting"}\n'
            '{"role": "user", "content": "help with the pipeline"}\n',
            encoding="utf-8",
        )
        import os
        os.utime(tmp_path / "title-match.jsonl", (1000, 1000))
        os.utime(tmp_path / "content-only.jsonl", (2000, 2000))
        log = ConversationLog(base_dir=tmp_path)
        results = log.search_sessions("apollo")
        assert [s["key"] for s in results] == ["title-match", "content-only"]

    def test_more_content_hits_ranks_higher(self, tmp_path):
        """Session with more content occurrences ranks above one with fewer (same length + no title match).

        Expected winner ``many`` is written *older* than ``few`` so that
        recency alone would place it second - only the hit-count scoring
        can flip the order.
        """
        (tmp_path / "many.jsonl").write_text(
            '{"_type": "metadata", "title": "t"}\n'
            '{"role": "user", "content": "apollo apollo apollo apollo apollo"}\n',
            encoding="utf-8",
        )
        (tmp_path / "few.jsonl").write_text(
            '{"_type": "metadata", "title": "t"}\n'
            '{"role": "user", "content": "apollo xxxxxx xxxxxx xxxxxx xxxxxx"}\n',
            encoding="utf-8",
        )
        import os
        os.utime(tmp_path / "many.jsonl", (1000, 1000))
        os.utime(tmp_path / "few.jsonl", (2000, 2000))
        log = ConversationLog(base_dir=tmp_path)
        results = log.search_sessions("apollo")
        assert [s["key"] for s in results] == ["many", "few"]

    def test_length_norm_favors_short_focused_session(self, tmp_path):
        """Short session with N hits ranks above long session with same N hits.

        Expected winner ``short`` is written *older* so recency alone
        would place it second - only length normalization can flip it.
        """
        (tmp_path / "short.jsonl").write_text(
            '{"_type": "metadata", "title": "t"}\n'
            '{"role": "user", "content": "apollo apollo apollo"}\n',
            encoding="utf-8",
        )
        (tmp_path / "long.jsonl").write_text(
            '{"_type": "metadata", "title": "t"}\n'
            '{"role": "user", "content": "apollo apollo apollo ' + "x " * 2000 + '"}\n',
            encoding="utf-8",
        )
        import os
        os.utime(tmp_path / "short.jsonl", (1000, 1000))
        os.utime(tmp_path / "long.jsonl", (2000, 2000))
        log = ConversationLog(base_dir=tmp_path)
        results = log.search_sessions("apollo")
        assert [s["key"] for s in results] == ["short", "long"]

    def test_zero_match_sessions_excluded(self, tmp_path):
        """Sessions without a match must not appear in results, even with limit>count."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("hit", "user", "apollo")
        log.append("miss", "user", "unrelated")
        assert [s["key"] for s in log.search_sessions("apollo")] == ["hit"]

    def test_recency_tiebreaker_when_scores_equal(self, tmp_path):
        """Equal-score sessions preserve recency order (newer first).

        Uses three sessions: a title-match (highest score) plus two
        content-only matches with identical score.  The middle higher-
        scoring entry forces the sort to actually reorder, so the
        newest-first-on-tie invariant isn't satisfied trivially.
        """
        (tmp_path / "older.jsonl").write_text(
            '{"_type": "metadata", "title": "t"}\n'
            '{"role": "user", "content": "apollo"}\n',
            encoding="utf-8",
        )
        (tmp_path / "middle-title.jsonl").write_text(
            '{"_type": "metadata", "title": "apollo"}\n'
            '{"role": "user", "content": "x"}\n',
            encoding="utf-8",
        )
        (tmp_path / "newer.jsonl").write_text(
            '{"_type": "metadata", "title": "t"}\n'
            '{"role": "user", "content": "apollo"}\n',
            encoding="utf-8",
        )
        import os
        os.utime(tmp_path / "older.jsonl", (1000, 1000))
        os.utime(tmp_path / "middle-title.jsonl", (1500, 1500))
        os.utime(tmp_path / "newer.jsonl", (2000, 2000))
        log = ConversationLog(base_dir=tmp_path)
        results = log.search_sessions("apollo")
        assert [s["key"] for s in results] == ["middle-title", "newer", "older"]

    def test_respects_limit_after_ranking(self, tmp_path):
        """*limit* caps results **after** ranking, so top-scored wins are kept.

        Expected winner ``strong`` is written *older* so recency alone
        would place it second (and an old early-exit-at-limit code path
        would return ``weak`` instead).
        """
        (tmp_path / "strong.jsonl").write_text(
            '{"_type": "metadata", "title": "t"}\n'
            '{"role": "user", "content": "apollo apollo apollo apollo apollo"}\n',
            encoding="utf-8",
        )
        (tmp_path / "weak.jsonl").write_text(
            '{"_type": "metadata", "title": "t"}\n'
            '{"role": "user", "content": "apollo and other things"}\n',
            encoding="utf-8",
        )
        import os
        os.utime(tmp_path / "strong.jsonl", (1000, 1000))
        os.utime(tmp_path / "weak.jsonl", (2000, 2000))
        log = ConversationLog(base_dir=tmp_path)
        results = log.search_sessions("apollo", limit=1)
        assert [s["key"] for s in results] == ["strong"]

    def test_title_boost_outranks_heavy_content(self, tmp_path):
        """Single title match outranks many content hits in a short session.

        Locks in the magnitude of ``_TITLE_BOOST``: if the constant is
        silently reduced (e.g. to 2), a short session with 5+ content
        hits would outrank a single title match and this test would
        fail.  Guards the "title is strong evidence" invariant.
        """
        # Short session with 5 content hits, no title match.  Written
        # directly so the title doesn't auto-extract from content.
        (tmp_path / "heavy-content.jsonl").write_text(
            '{"_type": "metadata", "title": "chat about deployments"}\n'
            '{"role": "user", "content": "apollo apollo apollo apollo apollo"}\n',
            encoding="utf-8",
        )
        # Title-only match, no content hits.  Written *older* so recency
        # alone would place it second - only the title boost can flip it.
        (tmp_path / "title-only.jsonl").write_text(
            '{"_type": "metadata", "title": "apollo deploy"}\n'
            '{"role": "user", "content": "unrelated text"}\n',
            encoding="utf-8",
        )
        import os
        os.utime(tmp_path / "title-only.jsonl", (1000, 1000))
        os.utime(tmp_path / "heavy-content.jsonl", (2000, 2000))
        log = ConversationLog(base_dir=tmp_path)
        results = log.search_sessions("apollo")
        assert results[0]["key"] == "title-only", (
            "A single title match must outrank even a heavy content-hit "
            "session - if this fails, _TITLE_BOOST was reduced below the "
            "threshold where title evidence dominates."
        )

    def test_scan_window_caps_files_scored(self, tmp_path, monkeypatch):
        """Only the ``_SEARCH_SCAN_WINDOW`` newest files are scored.

        Files outside the window must not appear in results even if they
        would score higher, bounding per-search I/O.
        """
        monkeypatch.setattr("kiro_crew.history._SEARCH_SCAN_WINDOW", 2)
        log = ConversationLog(base_dir=tmp_path)
        # Oldest: strong match (would win on score if scanned)
        log.append("old-strong", "user", "apollo apollo apollo apollo apollo")
        # Two newer weak matches fill the scan window
        log.append("new-weak-1", "user", "apollo x")
        log.append("new-weak-2", "user", "apollo y")
        # Explicit mtimes: filesystems with 1-second granularity (macOS
        # HFS+) can give all three files the same mtime, making the
        # list_sessions() order non-deterministic without this.
        import os
        os.utime(tmp_path / "old-strong.jsonl", (1000, 1000))
        os.utime(tmp_path / "new-weak-1.jsonl", (2000, 2000))
        os.utime(tmp_path / "new-weak-2.jsonl", (3000, 3000))
        result_keys = [s["key"] for s in log.search_sessions("apollo")]
        assert "old-strong" not in result_keys
        assert "new-weak-1" in result_keys
        assert "new-weak-2" in result_keys

    def test_substring_scan_reads_through_msg_cache(self, tmp_path):
        """search_sessions sources content via _read_messages (the mtime
        cache), not a second raw file parse.

        Regression lock for the double-parse fix: previously the scan opened
        and re-parsed each file inline, so _read_messages was never consulted
        by the search path (the snippet path then parsed the same file a
        second time). After the fix the scan goes through the cache, so a
        matched session is counted via _read_messages and its parse is
        memoized for the snippet read.
        """
        log = ConversationLog(base_dir=tmp_path)
        log.append("a", "user", "apollo deployment rollback notes")
        calls: list[str] = []
        real = log._read_messages

        def counting(key: str) -> list[dict]:
            calls.append(key)
            return real(key)

        log._read_messages = counting  # type: ignore[assignment]
        assert [s["key"] for s in log.search_sessions("apollo")] == ["a"]
        # The scan consulted the cache entry point for the matched session...
        assert "a" in calls
        # ...and the parse is now memoized, so the snippet read is a cache hit.
        assert "a" in log._msg_cache


class TestArchive:
    def test_rotate_archives_dropped_lines(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.history._SESSION_MAX_BYTES", 100)
        monkeypatch.setattr("kiro_crew.history._SESSION_KEEP_LINES", 3)
        log = ConversationLog(base_dir=tmp_path)
        for i in range(20):
            log.append("t1", "user", f"message number {i} with enough text to exceed limits")
        archives = list((tmp_path / "archive").glob("t1__*.jsonl"))
        assert len(archives) >= 1
        content = archives[0].read_text()
        header = json.loads(content.splitlines()[0])
        assert header["_type"] == "archive"
        assert header["reason"] == "rotate"
        assert header["count"] > 0

    def test_rewrite_session_archives_existing(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        log.append("t1", "user", "original msg 1")
        log.append("t1", "assistant", "original msg 2")
        log.rewrite_session("t1", [{"role": "user", "content": "new", "ts": "x"}])
        archives = list((tmp_path / "archive").glob("t1__*.jsonl"))
        assert len(archives) == 1
        content = archives[0].read_text()
        assert "original msg 1" in content
        assert "original msg 2" in content
        header = json.loads(content.splitlines()[0])
        assert header["reason"] == "compact"

    def test_cleanup_old_archives(self, tmp_path):
        import os
        import time

        import kiro_crew.history as history_mod
        from kiro_crew.history import _cleanup_old_archives

        history_mod._last_cleanup = 0.0  # reset rate-limit so cleanup actually runs
        adir = tmp_path / "archive"
        adir.mkdir()
        old = adir / "old__20200101-000000.jsonl"
        old.write_text("{}\n")
        new = adir / "new__20990101-000000.jsonl"
        new.write_text("{}\n")
        # Backdate old file by 10 days
        ten_days_ago = time.time() - 10 * 86400
        os.utime(old, (ten_days_ago, ten_days_ago))
        removed = _cleanup_old_archives(retention_days=7, base=tmp_path)
        assert removed == 1
        assert not old.exists()
        assert new.exists()

    def test_archive_empty_lines_noop(self, tmp_path):
        from kiro_crew.history import _archive_lines

        result = _archive_lines("k", [], reason="rotate", base=tmp_path)
        assert result is None
        assert not (tmp_path / "archive").exists()

    def test_same_second_conflict_suffixes_filename(self, tmp_path):
        """Multiple archives for same key in same second must not clobber each other."""
        from kiro_crew.history import _archive_lines

        p1 = _archive_lines("k", ["line1\n"], reason="rotate", base=tmp_path)
        p2 = _archive_lines("k", ["line2\n"], reason="rotate", base=tmp_path)
        p3 = _archive_lines("k", ["line3\n"], reason="rotate", base=tmp_path)
        assert len({p1, p2, p3}) == 3
        assert p1.exists() and p2.exists() and p3.exists()
        assert "line1" in p1.read_text()
        assert "line2" in p2.read_text()
        assert "line3" in p3.read_text()

    def test_cleanup_old_archives_noop_when_dir_missing(self, tmp_path):
        import kiro_crew.history as history_mod
        from kiro_crew.history import _cleanup_old_archives

        history_mod._last_cleanup = 0.0
        removed = _cleanup_old_archives(retention_days=7, base=tmp_path)
        assert removed == 0

    def test_cleanup_disabled_when_retention_negative(self, tmp_path):
        """retention_days < 0 disables cleanup — old files are kept."""
        import os
        import time

        import kiro_crew.history as history_mod
        from kiro_crew.history import _cleanup_old_archives

        history_mod._last_cleanup = 0.0
        adir = tmp_path / "archive"
        adir.mkdir()
        old = adir / "old__20200101-000000.jsonl"
        old.write_text("{}\n")
        ten_days_ago = time.time() - 10 * 86400
        os.utime(old, (ten_days_ago, ten_days_ago))
        removed = _cleanup_old_archives(retention_days=-1, base=tmp_path)
        assert removed == 0
        assert old.exists()

    def test_cleanup_resolves_retention_from_config(self, tmp_path, monkeypatch):
        """retention_days=None resolves the window from config."""
        import os
        import time

        import kiro_crew.history as history_mod
        from kiro_crew.history import _cleanup_old_archives

        monkeypatch.setattr(history_mod, "_resolve_retention_days", lambda: 7)
        history_mod._last_cleanup = 0.0
        adir = tmp_path / "archive"
        adir.mkdir()
        old = adir / "old__20200101-000000.jsonl"
        old.write_text("{}\n")
        ten_days_ago = time.time() - 10 * 86400
        os.utime(old, (ten_days_ago, ten_days_ago))
        removed = _cleanup_old_archives(base=tmp_path)
        assert removed == 1
        assert not old.exists()

    def test_cleanup_throttled_skips_config_load(self, tmp_path, monkeypatch):
        """A rate-limited call must NOT resolve retention from config (Bug #6).

        Config resolution (KiroCrewConfig.load — a disk read + parse) is
        expensive and runs on every archive write via _archive_lines. The
        throttle guard must short-circuit BEFORE that read so the common
        once-per-hour-already-ran path stays cheap.
        """
        import time

        import kiro_crew.history as history_mod
        from kiro_crew.history import _cleanup_old_archives

        def _boom() -> int:
            raise AssertionError("config must not be loaded on a throttled call")

        monkeypatch.setattr(history_mod, "_resolve_retention_days", _boom)
        # Simulate a cleanup that ran moments ago → within the 1h window.
        history_mod._last_cleanup = time.time()
        removed = _cleanup_old_archives(base=tmp_path)
        assert removed == 0

    def test_cleanup_throttled_explicit_negative_skips_config_load(
        self, tmp_path, monkeypatch
    ):
        """Explicit negative disables without touching config, even when throttled."""
        import time

        import kiro_crew.history as history_mod
        from kiro_crew.history import _cleanup_old_archives

        def _boom() -> int:
            raise AssertionError("config must not be loaded for explicit negative")

        monkeypatch.setattr(history_mod, "_resolve_retention_days", _boom)
        history_mod._last_cleanup = time.time()
        removed = _cleanup_old_archives(retention_days=-1, base=tmp_path)
        assert removed == 0

    def test_cleanup_config_disabled_stamps_window_to_throttle_next_call(
        self, tmp_path, monkeypatch
    ):
        """A config-resolved 'disabled' must still stamp _last_cleanup (Bug #6).

        If the throttle window is not stamped when retention resolves negative,
        every subsequent archive write re-runs the expensive config load. The
        first call should resolve config once; the immediate next call must be
        throttled and NOT resolve config again.
        """
        import kiro_crew.history as history_mod
        from kiro_crew.history import _cleanup_old_archives

        calls = {"n": 0}

        def _disabled() -> int:
            calls["n"] += 1
            return -1  # cleanup disabled via config

        monkeypatch.setattr(history_mod, "_resolve_retention_days", _disabled)
        history_mod._last_cleanup = 0.0  # force first call past the throttle
        assert _cleanup_old_archives(base=tmp_path) == 0
        assert calls["n"] == 1  # config resolved once
        # Immediate second call must be throttled → config NOT resolved again.
        assert _cleanup_old_archives(base=tmp_path) == 0
        assert calls["n"] == 1

    def test_safe_key_sanitizes_unsafe_chars(self, tmp_path):
        """Keys with slashes/colons must be sanitized into safe filenames."""
        from kiro_crew.history import _archive_lines, _safe_key

        assert _safe_key("slack:C123/456") == "slack_C123_456"
        p = _archive_lines("slack:C123/456", ["x\n"], reason="rotate", base=tmp_path)
        assert p is not None
        assert "/" not in p.name and ":" not in p.name
        assert p.name.startswith("slack_C123_456__")

    def test_multiple_rotations_produce_multiple_archives(self, tmp_path, monkeypatch):
        """A session that keeps growing across multiple rotate cycles produces multiple archive files."""
        monkeypatch.setattr("kiro_crew.history._SESSION_MAX_BYTES", 200)
        monkeypatch.setattr("kiro_crew.history._SESSION_KEEP_LINES", 2)
        log = ConversationLog(base_dir=tmp_path)
        for _ in range(3):
            # Each round writes enough to trigger a rotate
            for i in range(20):
                log.append("loop", "user", f"msg {i} " + "x" * 50)
        archives = list((tmp_path / "archive").glob("loop__*.jsonl"))
        assert len(archives) >= 2, f"expected multiple archives, got {len(archives)}"

    def test_archive_header_is_valid_json_metadata_line(self, tmp_path):
        """First line of archive is a JSON metadata row; remaining lines are original message jsonl."""
        from kiro_crew.history import _archive_lines

        p = _archive_lines("k", ['{"role":"user","content":"a"}\n', '{"role":"assistant","content":"b"}\n'], reason="rotate", base=tmp_path)
        lines = p.read_text().splitlines()
        header = json.loads(lines[0])
        assert header == {"_type": "archive", "reason": "rotate", "archived_at": header["archived_at"], "count": 2}
        assert json.loads(lines[1])["role"] == "user"
        assert json.loads(lines[2])["role"] == "assistant"


class TestArchiveDashboardAPI:
    """HTTP-level tests for /api/session/archive endpoints."""

    @staticmethod
    def _make_app():
        import pytest

        pytest.importorskip("aiohttp")
        from aiohttp import web

        from kiro_crew.dashboard.handlers import (
            api_session_archive_list,
            api_session_archive_read,
        )

        app = web.Application()
        app.router.add_get("/api/session/archive", api_session_archive_list)
        app.router.add_get("/api/session/archive/{name}", api_session_archive_read)
        # Handler resolves archive dir via _sessions_dir(); tests monkeypatch that.
        return app

    @pytest.fixture
    def archive_dir(self, tmp_path, monkeypatch):
        """Create an archive dir seeded with fake archive files and wire _sessions_dir()."""
        import os
        import time

        import kiro_crew.history as history_mod

        sessions = tmp_path / "sessions"
        archive = sessions / "archive"
        archive.mkdir(parents=True)
        now = time.time()
        # Oldest mtime
        (archive / "a__20260101-000000.jsonl").write_text(
            '{"_type":"archive","reason":"rotate","count":1}\n{"role":"user","content":"x"}\n'
        )
        os.utime(archive / "a__20260101-000000.jsonl", (now - 300, now - 300))
        # Newest mtime (should sort first)
        (archive / "b__20260102-000000.jsonl").write_text(
            '{"_type":"archive","reason":"compact","count":1}\n{"role":"user","content":"y"}\n'
        )
        os.utime(archive / "b__20260102-000000.jsonl", (now, now))
        # Middle mtime
        (archive / "a__20260103-000000.jsonl").write_text(
            '{"_type":"archive","reason":"rotate","count":1}\n{"role":"user","content":"z"}\n'
        )
        os.utime(archive / "a__20260103-000000.jsonl", (now - 100, now - 100))
        monkeypatch.setattr(history_mod, "_sessions_dir", lambda: sessions)
        return archive

    @pytest.mark.asyncio
    async def test_list_returns_all_archives(self, archive_dir):
        from aiohttp.test_utils import TestClient, TestServer

        app = self._make_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/session/archive")
            assert resp.status == 200
            data = await resp.json()
            assert len(data["archives"]) == 3
            # Sorted newest first by mtime (not filename)
            assert data["archives"][0]["name"] == "b__20260102-000000.jsonl"
            assert data["archives"][1]["name"] == "a__20260103-000000.jsonl"
            assert data["archives"][2]["name"] == "a__20260101-000000.jsonl"
            assert set(e["key"] for e in data["archives"]) == {"a", "b"}

    @pytest.mark.asyncio
    async def test_list_key_prefix_filter(self, archive_dir):
        from aiohttp.test_utils import TestClient, TestServer

        app = self._make_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/session/archive?key=a")
            data = await resp.json()
            assert len(data["archives"]) == 2
            assert all(e["key"] == "a" for e in data["archives"])

    @pytest.mark.asyncio
    async def test_list_empty_when_no_archive_dir(self, tmp_path, monkeypatch):
        from aiohttp.test_utils import TestClient, TestServer

        import kiro_crew.history as history_mod

        sessions = tmp_path / "sessions"
        sessions.mkdir()
        monkeypatch.setattr(history_mod, "_sessions_dir", lambda: sessions)
        app = self._make_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/session/archive")
            assert resp.status == 200
            data = await resp.json()
            assert data["archives"] == []

    @pytest.mark.asyncio
    async def test_read_returns_archive_content(self, archive_dir):
        from aiohttp.test_utils import TestClient, TestServer

        app = self._make_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/session/archive/a__20260101-000000.jsonl")
            assert resp.status == 200
            body = await resp.text()
            assert "x" in body and "archive" in body

    @pytest.mark.asyncio
    async def test_read_rejects_path_traversal(self, archive_dir):
        from aiohttp.test_utils import TestClient, TestServer

        app = self._make_app()
        async with TestClient(TestServer(app)) as client:
            # Names with '..' must be rejected by the handler's canonical path check.
            # '..' alone (no slash) reaches the handler; canonical-resolve check catches it.
            # URL-encoded slashes ('..%2Fetc.jsonl') may be rejected by the router (404)
            # or by the handler (400) depending on aiohttp version — both are acceptable.
            for bad, expected in [
                ("..", (400, 404)),  # missing .jsonl → 400, or no match → 404
                ("...jsonl", (400, 404)),  # may resolve inside dir → 404, or caught → 400
                ("..%2Fetc.jsonl", (400, 403, 404)),
                ("..%2F..%2Fetc.jsonl", (400, 403, 404)),
            ]:
                resp = await client.get(f"/api/session/archive/{bad}")
                assert resp.status in expected, f"{bad} returned {resp.status}"

    @pytest.mark.asyncio
    async def test_read_rejects_non_jsonl_extension(self, archive_dir):
        from aiohttp.test_utils import TestClient, TestServer

        # Put a forbidden file alongside archives
        (archive_dir / "secret.txt").write_text("SHOULD NOT BE READABLE")
        app = self._make_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/session/archive/secret.txt")
            assert resp.status in (400, 403, 404)

    @pytest.mark.asyncio
    async def test_read_missing_archive_returns_404(self, archive_dir):
        from aiohttp.test_utils import TestClient, TestServer

        app = self._make_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/session/archive/nonexistent.20260101-000000.jsonl")
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_read_redacts_credentials_and_urls(self, archive_dir):
        """Archived content is redacted (credentials + exfiltration URLs) before being served."""
        from aiohttp.test_utils import TestClient, TestServer

        # Write an archive containing a fake AWS access key
        leaky = archive_dir / "leak__20260104-000000.jsonl"
        leaky.write_text(
            '{"_type":"archive","reason":"rotate","count":1}\n'
            '{"role":"user","content":"here is AKIAIOSFODNN7EXAMPLE my key"}\n'
        )
        app = self._make_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/session/archive/leak__20260104-000000.jsonl")
            assert resp.status == 200
            body = await resp.text()
            # Raw credential must not appear in the response
            assert "AKIAIOSFODNN7EXAMPLE" not in body


class TestArchiveOnlyDropped:
    """rewrite_session must archive only the messages being dropped, not kept ones."""

    def test_rewrite_archives_only_dropped_messages(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        log.append("t1", "user", "A")
        log.append("t1", "assistant", "B")
        log.append("t1", "user", "C")
        # Read back the three message lines so we can feed them exactly to rewrite_session
        from kiro_crew.history import _safe_key

        path = tmp_path / f"{_safe_key('t1')}.jsonl"
        lines = [ln for ln in path.read_text().splitlines() if ln and '"_type"' not in ln]
        assert len(lines) == 3
        kept = [json.loads(lines[1]), json.loads(lines[2])]  # B, C
        log.rewrite_session("t1", kept)
        archives = list((tmp_path / "archive").glob("t1__*.jsonl"))
        assert len(archives) == 1
        archived = archives[0].read_text()
        # Only the dropped message A should be in the archive (not B or C).
        assert "\"content\": \"A\"" in archived
        assert "\"content\": \"B\"" not in archived
        assert "\"content\": \"C\"" not in archived
        header = json.loads(archived.splitlines()[0])
        assert header["count"] == 1


# ---------------------------------------------------------------------------
# Tests for Mesh-611: consolidation offset only advances on success
# ---------------------------------------------------------------------------


class TestConsolidationToolPolicy:
    """The background consolidation LLM turn must run tool-free on ALL providers.

    kiro scopes the kirocrew-lite session to tools:[] via set_mode, but the
    Claude Code backend skips set_mode and injects the full kirocrew-core/cron
    toolset (auto-approved). To keep parity — and prevent a background turn from
    firing side-effecting tools like send_message/learn_add — _call_llm must
    reject all tools regardless of provider.
    """

    @pytest.mark.asyncio
    async def test_call_llm_rejects_tools(self):
        from kiro_crew.llm_helpers import ToolApprovalPolicy

        provider = MagicMock()
        sessions = MagicMock()
        sessions.get_or_create = AsyncMock(return_value=(provider, False, False))
        sessions.release = MagicMock()
        sessions.recycle_background = AsyncMock()

        consolidator = HistoryConsolidator(log=MagicMock(), memory=MagicMock(), sessions=sessions)

        captured = {}

        async def _fake_scj(prov, prompt, *, approval_policy=None, **kw):
            captured["approval_policy"] = approval_policy
            return {"ok": True}

        # Patch where it is USED — history.py imports the symbol at module top.
        with patch("kiro_crew.history.stream_and_collect_json", side_effect=_fake_scj):
            result = await consolidator._call_llm("some prompt")

        assert result == {"ok": True}
        assert captured["approval_policy"] == ToolApprovalPolicy.REJECT_ALL, (
            "background consolidation turn must reject tools (parity with kiro's "
            "tool-free lite agent; CC injects the full toolset otherwise)"
        )


class TestConsolidationOffset:
    """Verify _prefs_offset only advances when _consolidate succeeds (Mesh-611)."""

    def _make_consolidator(self, msg_count=_CONSOLIDATION_THRESHOLD):
        log = MagicMock()
        log._read_messages = MagicMock(return_value=[{}] * msg_count)
        return HistoryConsolidator(log=log, memory=MagicMock(), sessions=None)

    def test_offset_advances_on_success(self):
        """When _consolidate succeeds, _prefs_offset should advance."""
        c = self._make_consolidator()

        async def run():
            with patch.object(c, "_consolidate", new_callable=AsyncMock):
                c.maybe_consolidate("k")
                await asyncio.gather(*c._tasks, return_exceptions=True)

        asyncio.run(run())
        assert c._prefs_offset.get("k") == _CONSOLIDATION_THRESHOLD

    def test_offset_does_not_advance_on_failure(self):
        """When _consolidate raises, _prefs_offset must NOT advance."""
        c = self._make_consolidator()

        async def run():
            with patch.object(c, "_consolidate", new_callable=AsyncMock) as m:
                m.side_effect = RuntimeError("LLM failed")
                c.maybe_consolidate("k")
                await asyncio.gather(*c._tasks, return_exceptions=True)

        asyncio.run(run())
        assert c._prefs_offset.get("k", 0) == 0

    def test_retry_after_failure(self):
        """After failure, next call retries (offset still 0)."""
        c = self._make_consolidator()

        async def run():
            with patch.object(c, "_consolidate", new_callable=AsyncMock) as m:
                m.side_effect = RuntimeError("timeout")
                c.maybe_consolidate("k")
                await asyncio.gather(*c._tasks, return_exceptions=True)
                c._running.discard("k")

                m.side_effect = None
                c.maybe_consolidate("k")
                await asyncio.gather(*c._tasks, return_exceptions=True)

        asyncio.run(run())
        assert c._prefs_offset["k"] == _CONSOLIDATION_THRESHOLD


class TestConsolidationDoesNotBlockLoop:
    """Structured-memory writes embed synchronously (blocking urllib to Ollama).

    _consolidate runs as an asyncio.create_task on the event loop thread, so it
    MUST offload _write_structured_memory to a worker thread — otherwise a slow
    or stalled embedding endpoint freezes the whole gateway loop (heartbeats,
    Slack, dashboard) and can trip the faulthandler hard-kill. Regression guard
    for the loop-stall crash traced to embeddings.py urlopen on the loop thread.
    """

    @pytest.mark.asyncio
    async def test_structured_memory_write_runs_off_loop_thread(self):
        import threading

        loop_thread_id = threading.get_ident()
        write_thread_id: dict[str, int] = {}

        log = MagicMock()
        log.get_unconsolidated.return_value = ([{"role": "user", "content": "hi"}], 1)
        log.get_metadata.return_value = {}

        memory = MagicMock()
        memory.read_preferences.return_value = ""
        memory.read_projects.return_value = ""

        vector_store = MagicMock()
        vector_store.get_all_semantic.return_value = []

        c = HistoryConsolidator(
            log=log, memory=memory, sessions=None,
            vector_store=vector_store, migrated=True,
        )

        def _fake_write(result, key):
            # Simulate the blocking embed call; record the executing thread.
            write_thread_id["id"] = threading.get_ident()

        with patch.object(c, "_call_llm", new_callable=AsyncMock) as llm, \
                patch.object(c, "_write_structured_memory", side_effect=_fake_write):
            llm.return_value = {"episodic": [{"text": "x" * 20}]}
            await c._consolidate("k", include_history=False)

        assert write_thread_id.get("id") is not None, "_write_structured_memory was not called"
        assert write_thread_id["id"] != loop_thread_id, (
            "_write_structured_memory ran on the event loop thread — a blocking "
            "embed here freezes the gateway loop. It must be offloaded via "
            "asyncio.to_thread()."
        )

    @pytest.mark.asyncio
    async def test_save_lessons_runs_off_loop_thread(self):
        """_save_lessons calls write_lesson which embeds via blocking urllib.

        Regression guard: 22475ceb offloaded _write_structured_memory but missed
        _save_lessons 15 lines below — the observed ~26s loop stall that causes
        learn_add MCP timeouts (collateral damage from the blocked event loop).
        """
        import threading

        loop_thread_id = threading.get_ident()
        save_thread_id: dict[str, int] = {}

        log = MagicMock()
        log.get_unconsolidated.return_value = ([{"role": "user", "content": "hi"}], 1)
        log.get_metadata.return_value = {}

        memory = MagicMock()
        memory.read_preferences.return_value = ""
        memory.read_projects.return_value = ""

        vector_store = MagicMock()
        vector_store.get_all_semantic.return_value = []
        vector_store.write_lesson.return_value = True

        c = HistoryConsolidator(
            log=log, memory=memory, sessions=None,
            vector_store=vector_store, migrated=True,
        )

        original_save = c._save_lessons

        def _instrumented_save(raw):
            save_thread_id["id"] = threading.get_ident()
            original_save(raw)

        with patch.object(c, "_call_llm", new_callable=AsyncMock) as llm, \
                patch.object(c, "_write_structured_memory"), \
                patch.object(c, "_save_lessons", side_effect=_instrumented_save):
            llm.return_value = {
                "lessons": [{"rule": "always check return codes", "category": "tool"}],
            }
            await c._consolidate("k", include_history=True)

        assert save_thread_id.get("id") is not None, "_save_lessons was not called"
        assert save_thread_id["id"] != loop_thread_id, (
            "_save_lessons ran on the event loop thread — write_lesson embeds via "
            "blocking urllib here, freezing the gateway loop. It must be offloaded "
            "via asyncio.to_thread()."
        )

    def test_save_lessons_caps_oversized_list(self):
        """The LLM lessons array is capped like semantic/episodic: each
        write_lesson can perform up to 6 blocking embeds, so an uncapped list
        would occupy a worker thread for minutes."""
        from kiro_crew.vector_memory import _MAX_LESSONS_PER_CONSOLIDATION

        vector_store = MagicMock()
        vector_store.write_lesson.return_value = True

        c = HistoryConsolidator(
            log=MagicMock(), memory=MagicMock(), sessions=None,
            vector_store=vector_store, migrated=True,
        )
        oversized = [
            {"rule": f"lesson number {i}", "category": "tool"}
            for i in range(_MAX_LESSONS_PER_CONSOLIDATION * 3)
        ]
        c._save_lessons(oversized)

        assert vector_store.write_lesson.call_count == _MAX_LESSONS_PER_CONSOLIDATION


class TestStopEventContextInjection:
    """Tests for context.py stop_event note injection."""

    def test_context_injection_stop_event(self, tmp_path):
        """context.py emits the system note for resolved stop events."""
        import json

        from kiro_crew.context import _build_stop_event_notes

        log = ConversationLog(base_dir=tmp_path)
        log.append("sess1", "user", "hello")
        log.append("sess1", "assistant", "hi")
        # Append a resolved stop_event as a system message
        stop_data = json.dumps({
            "kind": "stop_event",
            "id": "stop-abc",
            "state": "stopped",
            "outcome": "soft",
        })
        log.append("sess1", "system", stop_data)

        result = _build_stop_event_notes(log, "sess1")
        assert "[User stopped the previous turn mid-execution.]" in result

    def test_context_injection_caps_at_three(self, tmp_path):
        """At most 3 stop event notes are injected."""
        import json

        from kiro_crew.context import _build_stop_event_notes

        log = ConversationLog(base_dir=tmp_path)
        for i in range(5):
            stop_data = json.dumps({
                "kind": "stop_event",
                "id": f"stop-{i}",
                "state": "stopped",
                "outcome": "soft",
            })
            log.append("sess1", "system", stop_data)

        result = _build_stop_event_notes(log, "sess1")
        count = result.count(
            "[User stopped the previous turn mid-execution.]"
        )
        assert count == 3

    def test_context_injection_ignores_stopping_state(self, tmp_path):
        """Unresolved stop_events (state=stopping) are not injected."""
        import json

        from kiro_crew.context import _build_stop_event_notes

        log = ConversationLog(base_dir=tmp_path)
        stop_data = json.dumps({
            "kind": "stop_event",
            "id": "stop-abc",
            "state": "stopping",
            "outcome": None,
        })
        log.append("sess1", "system", stop_data)

        result = _build_stop_event_notes(log, "sess1")
        assert result == ""


class TestAutoSkillHelpers:
    """Module-level helpers for auto-skill eligibility (Mesh-677)."""

    def test_count_tool_call_messages(self):
        from kiro_crew.history import _count_tool_call_messages

        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello", "tools": ["fs_read"]},
            {"role": "user", "content": "do X"},
            {"role": "assistant", "content": "ok", "tools": ["fs_read", "execute_bash"]},
            {"role": "assistant", "content": "done", "tools": []},  # empty list counts as zero
            {"role": "assistant", "content": "another", "tools": ["fs_read"]},
        ]
        assert _count_tool_call_messages(messages) == 3

    def test_count_handles_malformed_tools(self):
        from kiro_crew.history import _count_tool_call_messages

        messages = [
            {"role": "assistant", "content": "x", "tools": "not-a-list"},
            {"role": "assistant", "content": "y"},
            {"role": "assistant", "content": "z", "tools": None},
        ]
        assert _count_tool_call_messages(messages) == 0

    def test_session_touched_sensitive_true_for_aws(self):
        from kiro_crew.history import _session_touched_sensitive

        messages = [
            {"role": "assistant", "content": "", "tools": ["Reading ~/.aws/credentials"]},
        ]
        assert _session_touched_sensitive(messages) is True

    def test_session_touched_sensitive_true_for_imds(self):
        from kiro_crew.history import _session_touched_sensitive

        messages = [
            {"role": "assistant", "content": "", "tools": ["curl 169.254.169.254/latest/..."]},
        ]
        assert _session_touched_sensitive(messages) is True

    def test_session_touched_sensitive_false_for_normal_tools(self):
        from kiro_crew.history import _session_touched_sensitive

        messages = [
            {"role": "assistant", "content": "", "tools": ["Running: ls /tmp", "fs_read"]},
            {"role": "assistant", "content": "", "tools": ["grep foo bar.txt"]},
        ]
        assert _session_touched_sensitive(messages) is False


class TestDashboardSchemaToolCallCounting:
    """Regression tests for dashboard-format tool messages (Mesh-677 schema fix)."""

    def test_count_dashboard_role_tool_messages(self):
        """Dashboard pipeline records tool calls as role='tool' messages."""
        from kiro_crew.history import _count_tool_call_messages

        messages = [
            {"role": "user", "content": "find info on grading"},
            {"role": "assistant", "content": "Let me look that up."},
            {"role": "tool", "content": "🔧 Running: @builder-mcp/ReadInternalWebsites"},
            {"role": "tool", "content": "✅ Running: @builder-mcp/ReadInternalWebsites"},
            {"role": "assistant", "content": "Here's what I found."},
            {"role": "tool", "content": "🔧 Running: @builder-mcp/InternalCodeSearch"},
            {"role": "tool", "content": "✅ Running: @builder-mcp/InternalCodeSearch"},
        ]
        assert _count_tool_call_messages(messages) == 4

    def test_sensitive_detection_dashboard_schema(self):
        """Sensitive paths in dashboard tool content are detected."""
        from kiro_crew.history import _session_touched_sensitive

        messages = [
            {"role": "assistant", "content": "Reading credentials."},
            {"role": "tool", "content": "🔧 Running: read ~/.aws/credentials"},
            {"role": "tool", "content": "✅ Running: read ~/.aws/credentials"},
        ]
        assert _session_touched_sensitive(messages) is True

    def test_sensitive_false_for_normal_dashboard_tools(self):
        """Normal dashboard tool messages don't trigger sensitive detection."""
        from kiro_crew.history import _session_touched_sensitive

        messages = [
            {"role": "tool", "content": "🔧 Running: @builder-mcp/ReadInternalWebsites"},
            {"role": "tool", "content": "✅ Running: @builder-mcp/InternalCodeSearch"},
        ]
        assert _session_touched_sensitive(messages) is False

    def test_mixed_schema_no_double_count(self):
        """Sessions mixing legacy tools field and dashboard role='tool' count correctly."""
        from kiro_crew.history import _count_tool_call_messages

        messages = [
            {"role": "assistant", "content": "step 1", "tools": ["fs_read"]},
            {"role": "tool", "content": "🔧 Running: @builder-mcp/ReadInternalWebsites"},
            {"role": "tool", "content": "✅ Running: @builder-mcp/ReadInternalWebsites"},
            {"role": "assistant", "content": "step 2", "tools": ["grep"]},
            # Edge case: a message with BOTH signals (shouldn't happen but test no double-count)
            {"role": "tool", "content": "tool msg", "tools": ["fs_read"]},
        ]
        # 2 legacy + 2 dashboard-only + 1 that has both (counted once via legacy branch) = 5
        assert _count_tool_call_messages(messages) == 5


class TestProcessAutoSkillsIntegration:
    """End-to-end consolidator path with flag off and flag on (mocked LLM)."""

    @pytest.mark.asyncio
    async def test_consolidator_default_off_never_writes(self, tmp_path):
        """With auto_skills_enabled=False (default), no skill writes happen."""
        from kiro_crew.memory import MemoryStore
        from kiro_crew.skills import SkillsLoader

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        mem = MemoryStore(workspace=tmp_path / "memory")
        mem.init()
        skills = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)

        consolidator = HistoryConsolidator(
            log=conv_log,
            memory=mem,
            skills_loader=skills,
            auto_skills_enabled=False,
        )

        # Seed a session with 10 tool calls — would be eligible if flag were on
        for i in range(10):
            conv_log.append("dashboard:chat-1", "assistant", f"step {i}", tools=["fs_read"])

        async def fake_llm(_prompt):
            return {
                "history_entry": "did 10 things",
                "new_skill": {
                    "slug": "should-not-be-written",
                    "description": "test",
                    "triggers": "t1, t2",
                    "procedure_md": "body",
                },
            }

        with patch.object(consolidator, "_call_llm", side_effect=fake_llm):
            await consolidator._consolidate("dashboard:chat-1", include_history=True)

        # Flag off → no auto skill written
        assert skills.list_auto_skills() == []

    @pytest.mark.asyncio
    async def test_consolidator_on_creates_auto_skill(self, tmp_path):
        from kiro_crew.memory import MemoryStore
        from kiro_crew.skills import SkillsLoader

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        mem = MemoryStore(workspace=tmp_path / "memory")
        mem.init()
        skills = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)

        consolidator = HistoryConsolidator(
            log=conv_log,
            memory=mem,
            skills_loader=skills,
            auto_skills_enabled=True,
            auto_min_tool_calls=5,
        )

        # 6 tool-call messages — above threshold, no sensitive paths
        for i in range(6):
            conv_log.append(
                "dashboard:chat-2", "assistant", f"step {i}", tools=["Running: grep foo bar.txt"]
            )

        async def fake_llm(_prompt):
            return {
                "history_entry": "did 6 things",
                "new_skill": {
                    "slug": "grep-with-context",
                    "description": "Search log files with grep then contextualize hits",
                    "triggers": "grep, log search, context lines",
                    "procedure_md": "## Steps\n1. grep -n pattern file\n2. Read ±5 lines\n",
                },
            }

        with patch.object(consolidator, "_call_llm", side_effect=fake_llm):
            await consolidator._consolidate("dashboard:chat-2", include_history=True)

        auto = skills.list_auto_skills()
        assert len(auto) == 1
        assert auto[0]["key"] == "auto/grep-with-context"
        skill_file = tmp_path / "skills" / "auto" / "grep-with-context" / "SKILL.md"
        assert skill_file.exists()
        content = skill_file.read_text()
        assert "source: auto" in content
        assert "session_key: dashboard:chat-2" in content
        assert "grep -n pattern file" in content

    @pytest.mark.asyncio
    async def test_sensitive_session_skipped_even_when_enabled(self, tmp_path):
        from kiro_crew.memory import MemoryStore
        from kiro_crew.skills import SkillsLoader

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        mem = MemoryStore(workspace=tmp_path / "memory")
        mem.init()
        skills = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)

        consolidator = HistoryConsolidator(
            log=conv_log,
            memory=mem,
            skills_loader=skills,
            auto_skills_enabled=True,
            auto_min_tool_calls=2,  # low threshold to force eligibility otherwise
        )

        for i in range(5):
            conv_log.append(
                "dashboard:chat-3",
                "assistant",
                f"step {i}",
                tools=["Reading ~/.aws/credentials"],
            )

        llm_called = False

        async def fake_llm(_prompt):
            nonlocal llm_called
            llm_called = True
            # The prompt built for this session should NOT include new_skill
            # because eligibility check failed.  Return basic keys only.
            return {"history_entry": "sensitive session"}

        with patch.object(consolidator, "_call_llm", side_effect=fake_llm):
            await consolidator._consolidate("dashboard:chat-3", include_history=True)

        assert llm_called  # consolidation still happened for memory
        # But no auto skill written
        assert skills.list_auto_skills() == []

    @pytest.mark.asyncio
    async def test_credentials_in_llm_output_are_redacted_before_write(self, tmp_path):
        """If the LLM returns a procedure with an AWS key, it's redacted before disk write."""
        from kiro_crew.memory import MemoryStore
        from kiro_crew.skills import SkillsLoader

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        mem = MemoryStore(workspace=tmp_path / "memory")
        mem.init()
        skills = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)

        consolidator = HistoryConsolidator(
            log=conv_log,
            memory=mem,
            skills_loader=skills,
            auto_skills_enabled=True,
            auto_min_tool_calls=2,
        )

        for i in range(5):
            conv_log.append("dashboard:chat-4", "assistant", f"step {i}", tools=["fs_read"])

        async def fake_llm(_prompt):
            return {
                "history_entry": "x",
                "new_skill": {
                    "slug": "poison-skill",
                    "description": "A procedure involving things",
                    "triggers": "thing, procedure",
                    "procedure_md": (
                        "## Steps\n"
                        "1. Use AKIAIOSFODNN7EXAMPLE as the key\n"
                        "2. Run `aws sts get-caller-identity`\n"
                    ),
                },
            }

        with patch.object(consolidator, "_call_llm", side_effect=fake_llm):
            await consolidator._consolidate("dashboard:chat-4", include_history=True)

        skill_file = tmp_path / "skills" / "auto" / "poison-skill" / "SKILL.md"
        assert skill_file.exists()
        content = skill_file.read_text()
        # AKIA prefix must NOT survive to disk
        assert "AKIAIOSFODNN7EXAMPLE" not in content

    @pytest.mark.asyncio
    async def test_similarity_dedup_skips_near_duplicate(self, tmp_path):
        from kiro_crew.memory import MemoryStore
        from kiro_crew.skills import SkillsLoader

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        mem = MemoryStore(workspace=tmp_path / "memory")
        mem.init()
        skills_dir = tmp_path / "skills"
        # Pre-existing skill we'd duplicate
        (skills_dir / "existing").mkdir(parents=True)
        (skills_dir / "existing" / "SKILL.md").write_text(
            "---\nname: existing\ndescription: Search timber logs via ssh chained patterns\n---\n"
        )
        skills = SkillsLoader(skills_path=skills_dir, install_builtins=False)

        consolidator = HistoryConsolidator(
            log=conv_log,
            memory=mem,
            skills_loader=skills,
            auto_skills_enabled=True,
            auto_min_tool_calls=2,
            auto_similarity_threshold=0.5,
        )

        for i in range(5):
            conv_log.append("dashboard:chat-5", "assistant", f"step {i}", tools=["fs_read"])

        async def fake_llm(_prompt):
            return {
                "history_entry": "x",
                "new_skill": {
                    "slug": "similar-timber-search",
                    # Near-duplicate description → should be deduped
                    "description": "Search timber logs via ssh chained patterns",
                    "triggers": "timber, log",
                    "procedure_md": "body",
                },
            }

        with patch.object(consolidator, "_call_llm", side_effect=fake_llm):
            await consolidator._consolidate("dashboard:chat-5", include_history=True)

        auto = skills.list_auto_skills()
        assert auto == []  # dedup prevented creation

    @pytest.mark.asyncio
    async def test_dashboard_schema_messages_trigger_auto_skill(self, tmp_path):
        """Dashboard-format role='tool' messages pass eligibility and produce a skill.

        This is the regression test that would have caught the schema mismatch bug.
        """
        from kiro_crew.memory import MemoryStore
        from kiro_crew.skills import SkillsLoader

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        mem = MemoryStore(workspace=tmp_path / "memory")
        mem.init()
        skills = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)

        consolidator = HistoryConsolidator(
            log=conv_log,
            memory=mem,
            skills_loader=skills,
            auto_skills_enabled=True,
            auto_min_tool_calls=5,
        )

        # Seed with REAL dashboard-format messages (no "tools" field anywhere)
        conv_log.append("dashboard:chat-schema", "user", "find info on grading services")
        conv_log.append("dashboard:chat-schema", "assistant", "Let me look that up.")
        conv_log.append("dashboard:chat-schema", "tool", "🔧 Running: @builder-mcp/ReadInternalWebsites")
        conv_log.append("dashboard:chat-schema", "tool", "✅ Running: @builder-mcp/ReadInternalWebsites")
        conv_log.append("dashboard:chat-schema", "assistant", "Now checking sub-pages.")
        conv_log.append("dashboard:chat-schema", "tool", "🔧 Running: @builder-mcp/ReadInternalWebsites")
        conv_log.append("dashboard:chat-schema", "tool", "✅ Running: @builder-mcp/ReadInternalWebsites")
        conv_log.append("dashboard:chat-schema", "tool", "🔧 Running: @builder-mcp/InternalCodeSearch")
        conv_log.append("dashboard:chat-schema", "tool", "✅ Running: @builder-mcp/InternalCodeSearch")
        conv_log.append("dashboard:chat-schema", "assistant", "Here's the full list.")

        async def fake_llm(_prompt):
            return {
                "history_entry": "explored grading services",
                "new_skill": {
                    "slug": "dashboard-wiki-explorer",
                    "description": "Navigate wiki sub-pages to enumerate services",
                    "triggers": "wiki, services, enumerate",
                    "procedure_md": "## Steps\n1. Read root wiki page\n2. Follow sub-links\n",
                },
            }

        with patch.object(consolidator, "_call_llm", side_effect=fake_llm):
            await consolidator._consolidate("dashboard:chat-schema", include_history=True)

        auto = skills.list_auto_skills()
        assert len(auto) == 1
        assert auto[0]["key"] == "auto/dashboard-wiki-explorer"


class TestAutoSkillSELAudit:
    """Regression test for AutoSDE findings #1-4: SEL audit must fire on rejection paths."""

    @pytest.mark.asyncio
    async def test_refine_namespace_lock_rejection_emits_sel(self, tmp_path):
        """When LLM tries to refine a hand-authored skill, SEL must log rejection."""
        from kiro_crew.memory import MemoryStore
        from kiro_crew.skills import SkillsLoader

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        mem = MemoryStore(workspace=tmp_path / "memory")
        mem.init()
        skills_dir = tmp_path / "skills"
        # Plant a hand-authored skill (not under auto/)
        (skills_dir / "manual-skill").mkdir(parents=True)
        (skills_dir / "manual-skill" / "SKILL.md").write_text(
            "---\nname: manual-skill\ndescription: hand-crafted\n---\n"
        )
        skills = SkillsLoader(skills_path=skills_dir, install_builtins=False)

        consolidator = HistoryConsolidator(
            log=conv_log,
            memory=mem,
            skills_loader=skills,
            auto_skills_enabled=True,
            auto_refine_enabled=True,
            auto_min_tool_calls=2,
        )

        for i in range(5):
            conv_log.append("dashboard:chat-refine", "assistant", f"s{i}", tools=["fs_read"])

        async def fake_llm(_prompt):
            return {
                "history_entry": "x",
                # LLM tries to refine a NON-auto skill (attack surface)
                "refined_skill": {
                    "name": "manual-skill",  # NOT under auto/
                    "description": "hijacked",
                    "triggers": "",
                    "procedure_md": "attacker content",
                },
            }

        recorded = []

        def fake_log(**kwargs):
            recorded.append(kwargs)

        with patch.object(consolidator, "_call_llm", side_effect=fake_llm):
            with patch("kiro_crew.history.sel") as mock_sel:
                mock_sel.return_value.log_tool_invocation = fake_log
                await consolidator._consolidate("dashboard:chat-refine", include_history=True)

        # Expect at least one audit entry with outcome=rejected and reason=not_auto_namespace
        namespace_rejections = [
            r for r in recorded
            if r.get("outcome") == "rejected"
            and r.get("metadata", {}).get("reason") == "not_auto_namespace"
        ]
        assert len(namespace_rejections) == 1
        assert namespace_rejections[0]["tool_name"] == "auto_skill_refine"
        assert namespace_rejections[0]["metadata"]["name"] == "manual-skill"
        # Original hand-authored skill untouched
        content = (skills_dir / "manual-skill" / "SKILL.md").read_text()
        assert "hand-crafted" in content
        assert "attacker content" not in content

    @pytest.mark.asyncio
    async def test_create_path_failure_emits_sel(self, tmp_path):
        """When create_auto_skill returns None (invalid slug / oversize), SEL must log rejection."""
        from kiro_crew.memory import MemoryStore
        from kiro_crew.skills import SkillsLoader

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        mem = MemoryStore(workspace=tmp_path / "memory")
        mem.init()
        skills = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)

        consolidator = HistoryConsolidator(
            log=conv_log,
            memory=mem,
            skills_loader=skills,
            auto_skills_enabled=True,
            auto_min_tool_calls=2,
        )

        for i in range(5):
            conv_log.append("dashboard:chat-bad-slug", "assistant", f"s{i}", tools=["fs_read"])

        async def fake_llm(_prompt):
            return {
                "history_entry": "x",
                "new_skill": {
                    "slug": "ab",  # Too short, fails regex
                    "description": "some description",
                    "triggers": "",
                    "procedure_md": "body",
                },
            }

        recorded = []

        def fake_log(**kwargs):
            recorded.append(kwargs)

        with patch.object(consolidator, "_call_llm", side_effect=fake_llm):
            with patch("kiro_crew.history.sel") as mock_sel:
                mock_sel.return_value.log_tool_invocation = fake_log
                await consolidator._consolidate("dashboard:chat-bad-slug", include_history=True)

        create_rejections = [
            r for r in recorded
            if r.get("tool_name") == "auto_skill_create"
            and r.get("outcome") == "rejected"
            and r.get("metadata", {}).get("reason") == "creation_failed"
        ]
        assert len(create_rejections) == 1
        assert create_rejections[0]["metadata"]["slug"] == "ab"
        # And no skill was written
        assert skills.list_auto_skills() == []


class TestAutoSkillSELAuditCompleteness:
    """Every no-write decision must emit a SEL audit event.

    Regression tests for AutoSDE round 2 findings — each distinct rejection
    branch in _process_auto_skills must surface via sel().log_tool_invocation.
    """

    @pytest.mark.asyncio
    async def test_create_empty_after_redaction_emits_sel(self, tmp_path):
        """If LLM returns new_skill but redaction strips everything, emit rejection audit."""
        from kiro_crew.memory import MemoryStore
        from kiro_crew.skills import SkillsLoader

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        mem = MemoryStore(workspace=tmp_path / "memory")
        mem.init()
        skills = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)

        consolidator = HistoryConsolidator(
            log=conv_log,
            memory=mem,
            skills_loader=skills,
            auto_skills_enabled=True,
            auto_min_tool_calls=2,
        )

        for i in range(5):
            conv_log.append("dashboard:chat-empty", "assistant", f"s{i}", tools=["fs_read"])

        async def fake_llm(_prompt):
            return {
                "history_entry": "x",
                "new_skill": {
                    "slug": "",  # empty slug — rejection before similarity check
                    "description": "",
                    "triggers": "",
                    "procedure_md": "",
                },
            }

        recorded: list[dict] = []

        def fake_log(**kwargs):
            recorded.append(kwargs)

        with patch.object(consolidator, "_call_llm", side_effect=fake_llm):
            with patch("kiro_crew.history.sel") as mock_sel:
                mock_sel.return_value.log_tool_invocation = fake_log
                await consolidator._consolidate("dashboard:chat-empty", include_history=True)

        empty_rejections = [
            r for r in recorded
            if r.get("tool_name") == "auto_skill_create"
            and r.get("outcome") == "rejected"
            and r.get("metadata", {}).get("reason") == "empty_after_redaction"
        ]
        assert len(empty_rejections) == 1
        assert skills.list_auto_skills() == []

    @pytest.mark.asyncio
    async def test_refine_empty_after_redaction_emits_sel(self, tmp_path):
        """Same gap on refine path: empty fields after redaction must audit."""
        from kiro_crew.memory import MemoryStore
        from kiro_crew.skills import AutoSkillProvenance, SkillsLoader

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        mem = MemoryStore(workspace=tmp_path / "memory")
        mem.init()
        skills = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
        # Plant a valid auto/ skill to refine
        skills.create_auto_skill(
            "existing-auto",
            description="existing desc",
            triggers="",
            procedure_md="v1",
            provenance=AutoSkillProvenance(
                session_key="seed", created_at="2026-05-05T11:00:00+00:00"
            ),
        )

        consolidator = HistoryConsolidator(
            log=conv_log,
            memory=mem,
            skills_loader=skills,
            auto_skills_enabled=True,
            auto_refine_enabled=True,
            auto_min_tool_calls=2,
        )

        for i in range(5):
            conv_log.append("dashboard:chat-refine-empty", "assistant", f"s{i}", tools=["fs_read"])

        async def fake_llm(_prompt):
            return {
                "history_entry": "x",
                "refined_skill": {
                    "name": "auto/existing-auto",
                    "description": "",  # empty — should trigger rejection audit
                    "triggers": "",
                    "procedure_md": "",
                },
            }

        recorded: list[dict] = []

        def fake_log(**kwargs):
            recorded.append(kwargs)

        with patch.object(consolidator, "_call_llm", side_effect=fake_llm):
            with patch("kiro_crew.history.sel") as mock_sel:
                mock_sel.return_value.log_tool_invocation = fake_log
                await consolidator._consolidate(
                    "dashboard:chat-refine-empty", include_history=True
                )

        empty_rejections = [
            r for r in recorded
            if r.get("tool_name") == "auto_skill_refine"
            and r.get("outcome") == "rejected"
            and r.get("metadata", {}).get("reason") == "empty_after_redaction"
        ]
        assert len(empty_rejections) == 1

    @pytest.mark.asyncio
    async def test_refine_update_failed_emits_sel(self, tmp_path):
        """When update_auto_skill returns False (oversized / missing), audit the rejection."""
        from kiro_crew.memory import MemoryStore
        from kiro_crew.skills import (
            AUTO_SKILL_MAX_PROCEDURE_CHARS,
            AutoSkillProvenance,
            SkillsLoader,
        )

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        mem = MemoryStore(workspace=tmp_path / "memory")
        mem.init()
        skills = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
        skills.create_auto_skill(
            "too-big-refine",
            description="original",
            triggers="",
            procedure_md="v1",
            provenance=AutoSkillProvenance(
                session_key="seed", created_at="2026-05-05T11:00:00+00:00"
            ),
        )

        consolidator = HistoryConsolidator(
            log=conv_log,
            memory=mem,
            skills_loader=skills,
            auto_skills_enabled=True,
            auto_refine_enabled=True,
            auto_min_tool_calls=2,
        )

        for i in range(5):
            conv_log.append("dashboard:chat-oversize", "assistant", f"s{i}", tools=["fs_read"])

        huge = "x" * (AUTO_SKILL_MAX_PROCEDURE_CHARS + 1)

        async def fake_llm(_prompt):
            return {
                "history_entry": "x",
                "refined_skill": {
                    "name": "auto/too-big-refine",
                    "description": "desc",
                    "triggers": "",
                    "procedure_md": huge,
                },
            }

        recorded: list[dict] = []

        def fake_log(**kwargs):
            recorded.append(kwargs)

        with patch.object(consolidator, "_call_llm", side_effect=fake_llm):
            with patch("kiro_crew.history.sel") as mock_sel:
                mock_sel.return_value.log_tool_invocation = fake_log
                await consolidator._consolidate("dashboard:chat-oversize", include_history=True)

        update_rejections = [
            r for r in recorded
            if r.get("tool_name") == "auto_skill_refine"
            and r.get("outcome") == "rejected"
            and r.get("metadata", {}).get("reason") == "update_failed"
        ]
        assert len(update_rejections) == 1


class TestConsolidationPromptJsonShape:
    """Regression test for AutoSDE round 2 finding #6: the new_skill prompt
    JSON shape example must itself be a valid JSON fragment so the LLM
    doesn't see an unclosed string and emit malformed output.

    We can't parse the whole prompt as JSON (it's English instructions
    containing JSON), but we CAN extract the shape example and verify
    every curly brace / quote is balanced.
    """

    def test_new_skill_prompt_shape_quotes_balanced(self):
        """Extract the new_skill shape example and verify balanced quotes."""
        import inspect

        from kiro_crew.history import HistoryConsolidator

        src = inspect.getsource(HistoryConsolidator._consolidate)
        # Find the new_skill prompt key block — it's a concatenated string
        # across multiple source lines.  Just verify the word-pair
        # '"description":' appears followed by a matching closing quote
        # within a handful of characters (i.e. not spanning multiple key
        # boundaries).
        assert '"description": "<=150 chars, starts with verb>",' in src, (
            "The description value in the new_skill prompt must end with "
            "a closing quote before the comma.  AutoSDE round 2 finding #6 "
            "caught this when it was missing; do not reintroduce."
        )
        # Same sanity check for procedure_md
        assert '"procedure_md": "<concise markdown body with' in src, (
            "procedure_md value must be a well-formed JSON string "
            "opener — don't split the value inside a quoted string."
        )


class TestConsolidateSession:
    """Tests for the public consolidate_session() method (session-end trigger)."""

    @pytest.mark.asyncio
    async def test_consolidate_session_fires_for_eligible(self, tmp_path):
        """consolidate_session triggers consolidation for sessions with messages."""
        from kiro_crew.memory import MemoryStore
        from kiro_crew.skills import SkillsLoader

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        mem = MemoryStore(workspace=tmp_path / "memory")
        mem.init()
        skills = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)

        consolidator = HistoryConsolidator(
            log=conv_log,
            memory=mem,
            skills_loader=skills,
            auto_skills_enabled=True,
            auto_min_tool_calls=2,
        )

        # Seed a session with tool calls
        for i in range(5):
            conv_log.append("dashboard:chat-expire", "assistant", f"s{i}", tools=["fs_read"])

        async def fake_llm(_prompt):
            return {
                "history_entry": "did stuff",
                "new_skill": {
                    "slug": "expire-triggered-skill",
                    "description": "Skill from session expire",
                    "triggers": "expire, test",
                    "procedure_md": "## Steps\n1. Do thing\n",
                },
            }

        with patch.object(consolidator, "_call_llm", side_effect=fake_llm):
            consolidator.consolidate_session("dashboard:chat-expire")
            # Let the task run
            await asyncio.sleep(0.1)
            # Drain pending tasks
            for t in list(consolidator._tasks):
                await t

        auto = skills.list_auto_skills()
        assert len(auto) == 1
        assert auto[0]["key"] == "auto/expire-triggered-skill"

    @pytest.mark.asyncio
    async def test_consolidate_session_skips_empty(self, tmp_path):
        """consolidate_session does nothing for sessions with no messages."""
        from kiro_crew.memory import MemoryStore

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        mem = MemoryStore(workspace=tmp_path / "memory")
        mem.init()

        consolidator = HistoryConsolidator(log=conv_log, memory=mem)

        # No messages for this key
        consolidator.consolidate_session("dashboard:chat-empty")
        # No tasks should be created
        assert len(consolidator._tasks) == 0

    @pytest.mark.asyncio
    async def test_consolidate_session_skips_already_running(self, tmp_path):
        """consolidate_session doesn't double-trigger for the same session."""
        from kiro_crew.memory import MemoryStore

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        mem = MemoryStore(workspace=tmp_path / "memory")
        mem.init()

        consolidator = HistoryConsolidator(log=conv_log, memory=mem)
        conv_log.append("dashboard:chat-dup", "user", "hello")

        # Simulate already running
        consolidator._running.add("dashboard:chat-dup")
        consolidator.consolidate_session("dashboard:chat-dup")
        # No new tasks created
        assert len(consolidator._tasks) == 0

    @pytest.mark.asyncio
    async def test_consolidate_session_skips_sensitive(self, tmp_path):
        """consolidate_session skips sessions that touched sensitive paths."""
        from kiro_crew.memory import MemoryStore

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        mem = MemoryStore(workspace=tmp_path / "memory")
        mem.init()

        consolidator = HistoryConsolidator(log=conv_log, memory=mem)
        # Seed a session with a sensitive tool call
        conv_log.append("dashboard:chat-sensitive", "assistant", "reading secrets", tools=["cat .aws/credentials"])

        consolidator.consolidate_session("dashboard:chat-sensitive")
        # No tasks created — sensitive session skipped
        assert len(consolidator._tasks) == 0
        assert "dashboard:chat-sensitive" not in consolidator._running

    @pytest.mark.asyncio
    async def test_consolidate_session_on_done_logs_exception(self, tmp_path):
        """_on_done callback logs warning when consolidation task raises."""
        from kiro_crew.memory import MemoryStore

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        mem = MemoryStore(workspace=tmp_path / "memory")
        mem.init()

        consolidator = HistoryConsolidator(log=conv_log, memory=mem)
        conv_log.append("dashboard:chat-fail", "user", "hello")

        with patch.object(consolidator, "_consolidate", new_callable=AsyncMock, side_effect=RuntimeError("boom")):
            consolidator.consolidate_session("dashboard:chat-fail")
            await asyncio.sleep(0.1)
            for t in list(consolidator._tasks):
                try:
                    await t
                except RuntimeError:
                    pass

        # Key should be removed from _running after _on_done fires
        assert "dashboard:chat-fail" not in consolidator._running

    @pytest.mark.asyncio
    async def test_consolidate_now_skips_sensitive(self, tmp_path):
        """consolidate_now skips sessions that touched sensitive paths."""
        from kiro_crew.memory import MemoryStore

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        mem = MemoryStore(workspace=tmp_path / "memory")
        mem.init()

        consolidator = HistoryConsolidator(log=conv_log, memory=mem)
        conv_log.append("dashboard:chat-sens2", "assistant", "read .ssh/id_rsa", tools=["cat .ssh/id_rsa"])

        with patch.object(consolidator, "_consolidate", new_callable=AsyncMock) as mock_consolidate:
            await consolidator.consolidate_now("dashboard:chat-sens2")
            mock_consolidate.assert_not_called()

    @pytest.mark.asyncio
    async def test_consolidate_now_happy_path(self, tmp_path):
        """consolidate_now calls _consolidate for eligible sessions."""
        from kiro_crew.memory import MemoryStore

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        mem = MemoryStore(workspace=tmp_path / "memory")
        mem.init()

        consolidator = HistoryConsolidator(log=conv_log, memory=mem)
        conv_log.append("dashboard:chat-ok", "user", "hello world")

        with patch.object(consolidator, "_consolidate", new_callable=AsyncMock) as mock_consolidate:
            await consolidator.consolidate_now("dashboard:chat-ok")
            mock_consolidate.assert_called_once_with("dashboard:chat-ok", include_history=True)

    @pytest.mark.asyncio
    async def test_consolidate_now_skips_empty(self, tmp_path):
        """consolidate_now does nothing for sessions with no unconsolidated messages."""
        from kiro_crew.memory import MemoryStore

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        mem = MemoryStore(workspace=tmp_path / "memory")
        mem.init()

        consolidator = HistoryConsolidator(log=conv_log, memory=mem)

        with patch.object(consolidator, "_consolidate", new_callable=AsyncMock) as mock_consolidate:
            await consolidator.consolidate_now("dashboard:chat-nonexist")
            mock_consolidate.assert_not_called()
