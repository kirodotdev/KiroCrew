"""Tests for conversation history module."""

from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from windows_sim import builtin_open_sharing_violation

from kiro_crew.history import (
    _CONSOLIDATION_THRESHOLD,
    _SESSION_KEEP_LINES,
    _SESSION_MAX_BYTES,
    ConversationLog,
    HistoryConsolidator,
)


class TestConversationLog:
    def test_append_creates_file(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        log.append("thread1", "user", "hello")
        path = tmp_path / "thread1.jsonl"
        assert path.exists()
        lines = path.read_text(encoding="utf-8").splitlines()
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
        lines = path.read_text(encoding="utf-8").splitlines()
        # Rotation keeps _SESSION_KEEP_LINES and then shrinks further until the
        # retained tail fits the byte budget, so the steady state is the cap plus
        # however many appends landed since the last rotation crossed it. That
        # overshoot is a function of a row's byte size, so assert the budget
        # rotation actually promises; the loose line bound is only here to catch
        # rotation not happening at all.
        assert path.stat().st_size <= _SESSION_MAX_BYTES
        assert len(lines) <= _SESSION_KEEP_LINES + 50

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

    def test_recent_from_source_sorted_by_ts(self, tmp_path, monkeypatch):
        import datetime as _dt

        # history stamps ts with datetime.now().isoformat(); on a coarse clock
        # (Windows' ~15ms tick) these rapid appends collide, so a ts-only sort
        # across sessions is ambiguous and the merge order leaks through (the
        # observed Windows failure: ['first', 'third', 'second']). Drive a
        # strictly-increasing clock so the chronological order the test asserts is
        # actually encoded in the timestamps, on every OS.
        _base = _dt.datetime(2026, 7, 25, 0, 0, 0, tzinfo=_dt.timezone.utc)
        _tick = {"n": 0}

        class _IncDateTime:
            @classmethod
            def now(cls, tz=None):
                _tick["n"] += 1
                return _base + _dt.timedelta(seconds=_tick["n"])

        monkeypatch.setattr("kiro_crew.history.datetime", _IncDateTime)

        log = ConversationLog(base_dir=tmp_path)
        # Append in different sessions — timestamps are strictly ordered.
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
        log.append("alpha", "user", "discussed CR-1234567 today")
        log.append("beta", "user", "unrelated chat")
        results = log.search_sessions("CR-1234567")
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

    def test_substring_scan_reads_the_file_and_leaves_msg_cache_alone(self, tmp_path):
        """search_sessions sources content from the file, never ``_msg_cache``.

        Both halves of a query read the session file directly: the fold that
        counts matches, and the snippet built for each returned row. Neither goes
        through ``_read_messages``, for two reasons.

        Memory: ``_read_messages`` memoizes the PARSED message dicts, and a scan
        touches every session in the window — so sourcing content through it makes
        searching pin the whole corpus's parsed form in RSS.

        Correctness: ``_msg_cache`` is populated by callers that hold no write
        lock, so an entry can be a pre-rewrite parse stored under a restored
        (unchanged) mtime. Folding from it would launder that staleness into the
        search cache, where the mtime guard cannot detect it.
        """
        log = ConversationLog(base_dir=tmp_path)
        log.append("a", "user", "apollo deployment rollback notes")
        log._msg_cache.clear()
        calls: list[str] = []
        real = log._read_messages

        def counting(key: str) -> list[dict]:
            calls.append(key)
            return real(key)

        log._read_messages = counting  # type: ignore[assignment]
        hits = log.search_sessions("apollo")
        assert [s["key"] for s in hits] == ["a"]
        # The snippet still resolves, from the file rather than a parsed cache.
        assert "apollo" in hits[0]["snippet"]
        assert calls == [], "the search path must not enter _read_messages"
        assert len(log._msg_cache) == 0, "searching must not pin a parsed transcript"


class TestArchive:
    def test_rotate_archives_dropped_lines(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.history._SESSION_MAX_BYTES", 100)
        monkeypatch.setattr("kiro_crew.history._SESSION_KEEP_LINES", 3)
        log = ConversationLog(base_dir=tmp_path)
        for i in range(20):
            log.append("t1", "user", f"message number {i} with enough text to exceed limits")
        archives = list((tmp_path / "archive").glob("t1__*.jsonl"))
        assert len(archives) >= 1
        content = archives[0].read_text(encoding="utf-8")
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
        content = archives[0].read_text(encoding="utf-8")
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
        assert "line1" in p1.read_text(encoding="utf-8")
        assert "line2" in p2.read_text(encoding="utf-8")
        assert "line3" in p3.read_text(encoding="utf-8")

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
        lines = p.read_text(encoding="utf-8").splitlines()
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
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln and '"_type"' not in ln]
        assert len(lines) == 3
        kept = [json.loads(lines[1]), json.loads(lines[2])]  # B, C
        log.rewrite_session("t1", kept)
        archives = list((tmp_path / "archive").glob("t1__*.jsonl"))
        assert len(archives) == 1
        archived = archives[0].read_text(encoding="utf-8")
        # Only the dropped message A should be in the archive (not B or C).
        assert "\"content\": \"A\"" in archived
        assert "\"content\": \"B\"" not in archived
        assert "\"content\": \"C\"" not in archived
        header = json.loads(archived.splitlines()[0])
        assert header["count"] == 1


# ---------------------------------------------------------------------------
# Tests for consolidation offset only advances on success
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
    """Verify _prefs_offset only advances when _consolidate succeeds."""

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
        log.snapshot_for_consolidation.return_value = (
            [{"role": "user", "content": "hi"}], 1, 0
        )
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
        log.snapshot_for_consolidation.return_value = (
            [{"role": "user", "content": "hi"}], 1, 0
        )
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
    """Module-level helpers for auto-skill eligibility."""

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
    """Regression tests for dashboard-format tool messages (schema fix)."""

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
            approval_required=False,
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
        content = skill_file.read_text(encoding="utf-8")
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
            approval_required=False,
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
            approval_required=False,
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
        content = skill_file.read_text(encoding="utf-8")
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
            approval_required=False,
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
            approval_required=False,
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

    @pytest.mark.asyncio
    async def test_consolidator_stages_when_approval_required(self, tmp_path):
        """With approval_required (the default), a new skill goes to the pending
        queue — not live — and is audited with outcome='staged'."""
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
            # approval_required defaults True → staged, not live
        )

        for i in range(6):
            conv_log.append(
                "dashboard:chat-stage", "assistant", f"step {i}", tools=["Running: grep foo bar.txt"]
            )

        async def fake_llm(_prompt):
            return {
                "history_entry": "did staged things",
                "new_skill": {
                    "slug": "staged-skill",
                    "description": "do a staged multi-step thing",
                    "triggers": "stage, staged",
                    "procedure_md": "## Steps\n1. go\n2. stop\n",
                },
            }

        with patch.object(consolidator, "_call_llm", side_effect=fake_llm):
            await consolidator._consolidate("dashboard:chat-stage", include_history=True)

        # Not live — staged instead.
        assert skills.list_auto_skills() == []
        pend = skills.list_pending_skills()
        assert [p["slug"] for p in pend] == ["staged-skill"]

    @pytest.mark.asyncio
    async def test_script_bearing_candidate_always_stages(self, tmp_path):
        """A clean script forces staging even when approval_required=False."""
        from kiro_crew.memory import MemoryStore
        from kiro_crew.skills import SkillsLoader

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        mem = MemoryStore(workspace=tmp_path / "memory")
        mem.init()
        skills = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
        consolidator = HistoryConsolidator(
            log=conv_log, memory=mem, skills_loader=skills,
            auto_skills_enabled=True, auto_min_tool_calls=2,
            approval_required=False,  # auto-approve prose — but scripts still gate
            generate_scripts=True,
        )
        for i in range(3):
            conv_log.append("dashboard:chat-scr", "assistant", f"s{i}", tools=["fs_read"])

        async def fake_llm(_p):
            return {
                "history_entry": "x",
                "new_skill": {
                    "slug": "scripted-skill",
                    "description": "run a fixed sequence",
                    "triggers": "seq",
                    "procedure_md": "## Steps\n1. run\n",
                    "scripts": [{"filename": "run.py", "language": "python", "content": "print('go')\n"}],
                },
            }

        with patch.object(consolidator, "_call_llm", side_effect=fake_llm):
            await consolidator._consolidate("dashboard:chat-scr", include_history=True)

        assert skills.list_auto_skills() == []  # not live despite auto-approve
        detail = skills.get_pending_skill("scripted-skill")
        assert detail is not None
        assert [s["filename"] for s in detail["scripts"]] == ["run.py"]

    @pytest.mark.asyncio
    async def test_dangerous_script_dropped_but_skill_staged(self, tmp_path):
        """A script failing the static validator is dropped; the skill still stages."""
        from kiro_crew.memory import MemoryStore
        from kiro_crew.skills import SkillsLoader

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        mem = MemoryStore(workspace=tmp_path / "memory")
        mem.init()
        skills = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
        consolidator = HistoryConsolidator(
            log=conv_log, memory=mem, skills_loader=skills,
            auto_skills_enabled=True, auto_min_tool_calls=2, generate_scripts=True,
        )
        for i in range(3):
            conv_log.append("dashboard:chat-bad", "assistant", f"s{i}", tools=["fs_read"])

        async def fake_llm(_p):
            return {
                "history_entry": "x",
                "new_skill": {
                    "slug": "dangerous-skill",
                    "description": "does a thing",
                    "triggers": "thing",
                    "procedure_md": "## Steps\n1. run\n",
                    "scripts": [{"filename": "wipe.py", "language": "python",
                                 "content": "import os\nos.system('rm -rf /')\n"}],
                },
            }

        with patch.object(consolidator, "_call_llm", side_effect=fake_llm):
            await consolidator._consolidate("dashboard:chat-bad", include_history=True)

        detail = skills.get_pending_skill("dangerous-skill")
        assert detail is not None  # skill still staged
        assert detail["scripts"] == []  # dangerous script dropped by validator


class TestAutoSkillSELAudit:
    """Regression test for review-bot findings #1-4: SEL audit must fire on rejection paths."""

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
            approval_required=False,
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
        content = (skills_dir / "manual-skill" / "SKILL.md").read_text(encoding="utf-8")
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
            approval_required=False,
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

    Regression tests for review-bot round 2 findings — each distinct rejection
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
            approval_required=False,
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
            approval_required=False,
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
            approval_required=False,
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
    """Regression test for review-bot round 2 finding #6: the new_skill prompt
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

        src = inspect.getsource(HistoryConsolidator._run_skill_detection)
        # Find the new_skill prompt key block — it's a concatenated string
        # across multiple source lines.  Just verify the word-pair
        # '"description":' appears followed by a matching closing quote
        # within a handful of characters (i.e. not spanning multiple key
        # boundaries).
        assert '"description": "<=150 chars, starts with verb>",' in src, (
            "The description value in the new_skill prompt must end with "
            "a closing quote before the comma.  review-bot round 2 finding #6 "
            "caught this when it was missing; do not reintroduce."
        )
        # Same sanity check for procedure_md
        assert '"procedure_md": "<concise markdown body with' in src, (
            "procedure_md value must be a well-formed JSON string "
            "opener — don't split the value inside a quoted string."
        )


class TestSkillDetectionFullWindow:
    """Skill detection judges the full-session window, not the consolidated tail.

    Regression for the tail-only recall gap: a reusable procedure that was
    already consolidated away (offset advanced past it) must still be seen by
    skill detection, because it reads the last-N of the FULL session rather
    than only the unconsolidated tail.
    """

    @pytest.mark.asyncio
    async def test_detects_from_full_window_when_tail_trivial(self, tmp_path):
        import asyncio as _asyncio
        from unittest.mock import patch

        from kiro_crew.memory import MemoryStore
        from kiro_crew.skills import SkillsLoader

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        mem = MemoryStore(workspace=tmp_path / "memory")
        mem.init()
        skills = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
        c = HistoryConsolidator(
            log=conv_log,
            memory=mem,
            skills_loader=skills,
            auto_skills_enabled=True,
            approval_required=True,
            auto_min_tool_calls=5,
        )
        key = "dashboard:chat-fullwindow"
        # 6 tool-bearing messages = the reusable procedure...
        for i in range(6):
            conv_log.append(key, "assistant", f"step {i}", tools=["execute_bash"])
        # ...already consolidated away: advance the offset past them.
        conv_log.mark_consolidated(key, 6)
        # A trivial, tool-free tail. Tail-only detection would see 0 tool calls
        # (< the 5 floor) and skip; full-window detection still sees the 6.
        conv_log.append(key, "user", "thanks!")
        conv_log.append(key, "assistant", "you're welcome")
        assert conv_log.unconsolidated_count(key) == 2  # the trivial tail

        recorded: dict = {}

        def fake_process(result, k):
            recorded["result"], recorded["key"] = result, k

        c._event_loop = _asyncio.get_running_loop()
        with patch.object(c, "_call_llm", return_value={"new_skill": {"slug": "x"}}):
            with patch.object(c, "_process_auto_skills", side_effect=fake_process):
                await c._run_skill_detection(key)
        assert recorded.get("key") == key, (
            "skill detection must fire from the full-session window even when the "
            "unconsolidated tail is trivial"
        )

    @pytest.mark.asyncio
    async def test_length_guard_skips_unchanged_session(self, tmp_path):
        import asyncio as _asyncio
        from unittest.mock import patch

        from kiro_crew.memory import MemoryStore
        from kiro_crew.skills import SkillsLoader

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        mem = MemoryStore(workspace=tmp_path / "memory")
        mem.init()
        skills = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
        c = HistoryConsolidator(
            log=conv_log,
            memory=mem,
            skills_loader=skills,
            auto_skills_enabled=True,
            approval_required=True,
            auto_min_tool_calls=5,
        )
        key = "dashboard:chat-guard"
        for i in range(6):
            conv_log.append(key, "assistant", f"step {i}", tools=["execute_bash"])
        c._event_loop = _asyncio.get_running_loop()
        with patch.object(c, "_call_llm", return_value=None) as m1:
            with patch.object(c, "_process_auto_skills"):
                await c._run_skill_detection(key)
                assert m1.call_count == 1  # first pass evaluates
                await c._run_skill_detection(key)
                assert m1.call_count == 1, (
                    "an unchanged session must NOT be re-judged on the next "
                    "consolidation (length guard)"
                )

    @pytest.mark.asyncio
    async def test_rotation_forces_fresh_pass_despite_equal_count(self, tmp_path):
        """A transcript rotation (generation bump) must re-trigger detection
        even when the message count is unchanged (GPT 5.6 blocking finding)."""
        import asyncio as _asyncio
        import json as _json
        from unittest.mock import patch

        from kiro_crew.memory import MemoryStore
        from kiro_crew.skills import SkillsLoader

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        mem = MemoryStore(workspace=tmp_path / "memory")
        mem.init()
        skills = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
        c = HistoryConsolidator(
            log=conv_log,
            memory=mem,
            skills_loader=skills,
            auto_skills_enabled=True,
            approval_required=True,
            auto_min_tool_calls=5,
        )
        key = "dashboard:chat-rotate"
        for i in range(6):
            conv_log.append(key, "assistant", f"step {i}", tools=["execute_bash"])
        c._event_loop = _asyncio.get_running_loop()
        with patch.object(c, "_call_llm", return_value=None) as m1:
            with patch.object(c, "_process_auto_skills"):
                await c._run_skill_detection(key)
                assert m1.call_count == 1
                # Simulate a 2MB/200-line rotation: same message count, but the
                # rotation_generation counter bumps and the window is new content.
                path = conv_log._path(key)
                lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
                meta = _json.loads(lines[0])
                meta["rotation_generation"] = int(meta.get("rotation_generation", 0) or 0) + 1
                lines[0] = _json.dumps(meta) + "\n"
                path.write_text("".join(lines), encoding="utf-8")
                conv_log._invalidate_cache(key)
                await c._run_skill_detection(key)
                assert m1.call_count == 2, (
                    "a rotation (generation bump) must force a fresh detection "
                    "pass even when the message count is unchanged"
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
            approval_required=False,
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


# ---------------------------------------------------------------------------
# PR unit C — recommendation #3: bounded LRU transcript/metadata caches
# ---------------------------------------------------------------------------


class TestLRUCache:
    """Unit tests for the bounded _LRUCache primitive backing the caches."""

    def test_hit_returns_value(self):
        from kiro_crew.history import _LRUCache

        c: _LRUCache[int] = _LRUCache(maxsize=4)
        c["a"] = 1
        assert c.get("a") == 1
        assert c["a"] == 1

    def test_miss_returns_default(self):
        from kiro_crew.history import _LRUCache

        c: _LRUCache[int] = _LRUCache(maxsize=4)
        assert c.get("missing") is None
        assert c.get("missing", 42) == 42

    def test_eviction_is_deterministic_lru(self):
        from kiro_crew.history import _LRUCache

        c: _LRUCache[int] = _LRUCache(maxsize=3)
        c["a"] = 1
        c["b"] = 2
        c["c"] = 3
        # Insert a 4th → 'a' (least recently used) is evicted.
        c["d"] = 4
        assert "a" not in c
        assert set(c._data.keys()) == {"b", "c", "d"}

    def test_get_marks_recently_used(self):
        from kiro_crew.history import _LRUCache

        c: _LRUCache[int] = _LRUCache(maxsize=3)
        c["a"] = 1
        c["b"] = 2
        c["c"] = 3
        # Touch 'a' so it is no longer the LRU victim.
        assert c.get("a") == 1
        c["d"] = 4
        # 'b' (now the LRU) is evicted instead of the touched 'a'.
        assert "b" not in c
        assert "a" in c

    def test_setitem_update_marks_recently_used(self):
        from kiro_crew.history import _LRUCache

        c: _LRUCache[int] = _LRUCache(maxsize=3)
        c["a"] = 1
        c["b"] = 2
        c["c"] = 3
        c["a"] = 10  # re-write existing key → most recently used
        c["d"] = 4
        assert "b" not in c
        assert c.get("a") == 10

    def test_pop_and_contains_and_len(self):
        from kiro_crew.history import _LRUCache

        c: _LRUCache[int] = _LRUCache(maxsize=4)
        c["a"] = 1
        c["b"] = 2
        assert len(c) == 2
        assert "a" in c
        assert c.pop("a") == 1
        assert "a" not in c
        assert c.pop("a", 99) == 99
        assert len(c) == 1

    def test_clear(self):
        from kiro_crew.history import _LRUCache

        c: _LRUCache[int] = _LRUCache(maxsize=4)
        c["a"] = 1
        c["b"] = 2
        c.clear()
        assert len(c) == 0

    def test_maxsize_zero_disables_bound(self):
        from kiro_crew.history import _LRUCache

        c: _LRUCache[int] = _LRUCache(maxsize=0)
        for i in range(1000):
            c[str(i)] = i
        assert len(c) == 1000


class TestConversationLogCacheBounded:
    """The message/metadata caches on ConversationLog are LRU-bounded."""

    def test_msg_cache_evicts_beyond_bound(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path, cache_max=4)
        for i in range(10):
            key = f"sess{i}"
            log.append(key, "user", f"hi {i}")
            log._read_messages(key)  # populate cache
        # Never exceeds the configured bound despite 10 distinct sessions.
        assert len(log._msg_cache) <= 4

    def test_meta_cache_evicts_beyond_bound(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path, cache_max=3)
        for i in range(10):
            key = f"sess{i}"
            log.append(key, "user", f"hi {i}")
            log.get_metadata(key)  # populate meta cache
        assert len(log._meta_cache) <= 3

    def test_cache_hit_returns_same_object(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path, cache_max=8)
        log.append("t1", "user", "a")
        first = log._read_messages("t1")
        second = log._read_messages("t1")
        # A cache hit returns the identical cached list object (no re-parse).
        assert first is second

    def test_write_invalidates_cache(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path, cache_max=8)
        log.append("t1", "user", "a")
        first = log._read_messages("t1")
        assert len(first) == 1
        log.append("t1", "assistant", "b")  # write must invalidate
        second = log._read_messages("t1")
        assert len(second) == 2
        assert first is not second

    def test_evicted_then_reread_is_correct(self, tmp_path):
        """A key evicted from the LRU is re-read correctly from disk."""
        log = ConversationLog(base_dir=tmp_path, cache_max=2)
        for i in range(5):
            log.append(f"s{i}", "user", f"content {i}")
            log._read_messages(f"s{i}")
        # s0 was evicted long ago; re-reading returns correct content.
        msgs = log._read_messages("s0")
        assert len(msgs) == 1
        assert msgs[0]["content"] == "content 0"


# ---------------------------------------------------------------------------
# PR unit C — recommendation #10: tail/seek reads for recent-only access
# ---------------------------------------------------------------------------


class TestTailReads:
    """recent() may serve a cache miss by reading only the file tail."""

    def test_recent_matches_full_read(self, tmp_path):
        """Tail-read recent() is byte-for-byte equivalent to the full-parse path."""
        log = ConversationLog(base_dir=tmp_path)
        for i in range(200):
            log.append("t1", "user" if i % 2 == 0 else "assistant", f"message {i}")
        # Force a cold cache so recent() takes the tail path.
        log._msg_cache.clear()
        got = log.recent("t1", max_messages=10)
        # Compare against the authoritative full read.
        log._msg_cache.clear()
        full = log._read_messages("t1")
        expected = [{"role": m["role"], "content": m["content"]} for m in full[-10:]]
        assert got == expected
        assert got[0]["content"] == "message 190"
        assert got[-1]["content"] == "message 199"

    def test_recent_role_filter_matches_full_read(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        for i in range(200):
            log.append("t1", "user" if i % 2 == 0 else "assistant", f"m{i}")
        log._msg_cache.clear()
        got = log.recent("t1", max_messages=5, roles={"assistant"})
        log._msg_cache.clear()
        full = log._read_messages("t1")
        filtered = [m for m in full if m["role"] == "assistant"]
        expected = [{"role": m["role"], "content": m["content"]} for m in filtered[-5:]]
        assert got == expected
        assert all(m["role"] == "assistant" for m in got)

    def test_tail_does_not_populate_full_cache(self, tmp_path):
        """A tail read must not leave a PARTIAL list in _msg_cache."""
        log = ConversationLog(base_dir=tmp_path)
        for i in range(100):
            log.append("t1", "user", f"m{i}")
        log._msg_cache.clear()
        log.recent("t1", max_messages=3)
        # Tail path returned a partial view — the full cache must stay empty
        # so load_transcript()/search still parse the whole file.
        assert "t1" not in log._msg_cache

    def test_tail_window_grows_when_insufficient(self, tmp_path):
        """When the initial window holds too few messages, it grows to satisfy the request."""
        log = ConversationLog(base_dir=tmp_path)
        # 300 large messages so the 8 KiB starting window can't hold 50 of them.
        for i in range(300):
            log.append("t1", "user", f"msg{i} " + "x" * 300)
        log._msg_cache.clear()
        got = log.recent("t1", max_messages=50)
        assert len(got) == 50
        assert got[-1]["content"].startswith("msg299 ")
        assert got[0]["content"].startswith("msg250 ")

    def test_tail_read_small_file(self, tmp_path):
        """A file smaller than one window is fully covered and correct."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("t1", "user", "only one")
        log._msg_cache.clear()
        got = log.recent("t1", max_messages=10)
        assert got == [{"role": "user", "content": "only one"}]

    def test_recent_nonexistent_session(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        assert log.recent("does-not-exist", max_messages=5) == []

    def test_fresh_full_cache_preferred_over_tail(self, tmp_path):
        """When a fresh full cache exists, recent() uses it (no tail read)."""
        log = ConversationLog(base_dir=tmp_path)
        for i in range(20):
            log.append("t1", "user", f"m{i}")
        log._read_messages("t1")  # warm the full cache
        # _read_tail_messages must NOT be consulted on this fresh-cache path.
        called = {"n": 0}
        real = log._read_tail_messages

        def spy(*a, **k):
            called["n"] += 1
            return real(*a, **k)

        log._read_tail_messages = spy  # type: ignore[assignment]
        got = log.recent("t1", max_messages=5)
        assert called["n"] == 0
        assert got[-1]["content"] == "m19"

    def test_exclude_last_n_uses_full_path(self, tmp_path):
        """exclude_last_n is only handled by the full-read path (tail bypassed)."""
        log = ConversationLog(base_dir=tmp_path)
        for i in range(10):
            log.append("t1", "user", f"m{i}")
        log._msg_cache.clear()
        got = log.recent("t1", max_messages=3, exclude_last_n=2)
        # Drops m8,m9 then takes last 3 of m0..m7 → m5,m6,m7
        assert [m["content"] for m in got] == ["m5", "m6", "m7"]

    def test_tail_reads_toggle_off(self, tmp_path):
        """Disabling _tail_reads forces the full-read path (behaviour identical)."""
        log = ConversationLog(base_dir=tmp_path)
        log._tail_reads = False
        for i in range(30):
            log.append("t1", "user", f"m{i}")
        log._msg_cache.clear()
        got = log.recent("t1", max_messages=4)
        # Full path populates the cache; result still correct.
        assert "t1" in log._msg_cache
        assert [m["content"] for m in got] == ["m26", "m27", "m28", "m29"]

    def test_tail_skips_metadata_line(self, tmp_path):
        """The metadata line is never surfaced as a message via the tail path."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("t1", "user", "first")  # creates metadata + message
        log._msg_cache.clear()
        got = log.recent("t1", max_messages=10)
        assert all(m.get("role") != "metadata" for m in got)
        assert got == [{"role": "user", "content": "first"}]


# ---------------------------------------------------------------------------
# Finding 0 / 4 — _LRUCache thread safety under concurrent access.
# ---------------------------------------------------------------------------


class TestLRUCacheConcurrency:
    """The bounded cache is touched from the event loop AND worker threads.

    These stress tests hammer the compound read-modify-write ops (move_to_end
    + index in get/__getitem__; the eviction len()+popitem loop in
    __setitem__) concurrently with pop/clear. Before the lock fix, a pop
    landing between a successful move_to_end and the following index raised
    KeyError out of get(); these tests would surface it as an escaped
    exception. After the fix, no exception escapes.
    """

    def test_get_pop_interleave_no_exception(self):
        import threading

        from kiro_crew.history import _LRUCache

        c: _LRUCache[int] = _LRUCache(maxsize=64)
        errors: list[BaseException] = []
        stop = threading.Event()

        def writer() -> None:
            i = 0
            while not stop.is_set():
                try:
                    c[str(i % 128)] = i
                    i += 1
                except BaseException as e:  # noqa: BLE001
                    errors.append(e)

        def reader() -> None:
            i = 0
            while not stop.is_set():
                try:
                    # get() must NEVER raise KeyError even if the key is
                    # concurrently popped in the move_to_end/index gap.
                    c.get(str(i % 128))
                    _ = str(i % 128) in c
                    i += 1
                except BaseException as e:  # noqa: BLE001
                    errors.append(e)

        def popper() -> None:
            i = 0
            while not stop.is_set():
                try:
                    c.pop(str(i % 128), None)
                    i += 1
                except BaseException as e:  # noqa: BLE001
                    errors.append(e)

        def clearer() -> None:
            while not stop.is_set():
                try:
                    len(c)
                    c.clear()
                except BaseException as e:  # noqa: BLE001
                    errors.append(e)

        threads = [
            threading.Thread(target=fn)
            for fn in (writer, writer, reader, reader, reader, popper, clearer)
        ]
        for t in threads:
            t.start()
        import time as _t

        _t.sleep(0.75)
        stop.set()
        for t in threads:
            t.join(timeout=5)
        assert not errors, f"cache raised under concurrency: {errors[:3]}"
        # Bound is still respected after the storm.
        assert len(c) <= 64

    def test_getitem_pop_interleave_no_exception(self):
        import threading

        from kiro_crew.history import _LRUCache

        c: _LRUCache[int] = _LRUCache(maxsize=32)
        for i in range(32):
            c[str(i)] = i
        errors: list[BaseException] = []
        stop = threading.Event()

        def indexer() -> None:
            i = 0
            while not stop.is_set():
                try:
                    try:
                        _ = c[str(i % 64)]
                    except KeyError:
                        pass  # a legitimate miss is fine; a race crash is not
                    i += 1
                except BaseException as e:  # noqa: BLE001
                    errors.append(e)

        def churner() -> None:
            i = 0
            while not stop.is_set():
                try:
                    c[str(i % 64)] = i
                    c.pop(str((i + 1) % 64), None)
                    i += 1
                except BaseException as e:  # noqa: BLE001
                    errors.append(e)

        threads = [threading.Thread(target=fn) for fn in (indexer, indexer, churner, churner)]
        for t in threads:
            t.start()
        import time as _t

        _t.sleep(0.5)
        stop.set()
        for t in threads:
            t.join(timeout=5)
        assert not errors, f"__getitem__ raised under concurrency: {errors[:3]}"


class TestConversationLogConcurrency:
    """append()/_invalidate_cache interleaved with recent()/read_messages().

    Exercises the ConversationLog-level shared state (message/meta/recent LRUs
    and the tab_id index) from multiple threads at once. No exception must
    escape and results must stay well-formed.
    """

    def test_append_read_recent_interleave(self, tmp_path):
        import threading

        log = ConversationLog(base_dir=tmp_path, cache_max=8)
        # Seed a few sessions.
        for s in range(4):
            for i in range(10):
                log.append(f"s{s}", "user" if i % 2 == 0 else "assistant", f"m{i}")
        errors: list[BaseException] = []
        stop = threading.Event()

        def appender(sid: int) -> None:
            i = 0
            while not stop.is_set():
                try:
                    log.append(f"s{sid}", "user", f"more {i}")
                    i += 1
                except BaseException as e:  # noqa: BLE001
                    errors.append(e)

        def recenter(sid: int) -> None:
            while not stop.is_set():
                try:
                    r = log.recent(f"s{sid}", max_messages=5)
                    assert isinstance(r, list)
                    r2 = log.recent(f"s{sid}", max_messages=5, roles={"assistant"})
                    assert isinstance(r2, list)
                except BaseException as e:  # noqa: BLE001
                    errors.append(e)

        def reader(sid: int) -> None:
            while not stop.is_set():
                try:
                    msgs = log.read_messages(f"s{sid}")
                    assert isinstance(msgs, list)
                except BaseException as e:  # noqa: BLE001
                    errors.append(e)

        threads = []
        for sid in range(4):
            threads.append(threading.Thread(target=appender, args=(sid,)))
            threads.append(threading.Thread(target=recenter, args=(sid,)))
            threads.append(threading.Thread(target=reader, args=(sid,)))
        for t in threads:
            t.start()
        import time as _t

        _t.sleep(0.75)
        stop.set()
        for t in threads:
            t.join(timeout=5)
        assert not errors, f"ConversationLog raised under concurrency: {errors[:3]}"

    def test_chained_read_invalidate_interleave(self, tmp_path):
        """read_messages_chained() vs invalidate_tab_id_cache() across threads."""
        import threading

        log = ConversationLog(base_dir=tmp_path)
        # Create dashboard sessions sharing a tab_id so the chained path builds
        # the tab_id index.
        for n in range(3):
            log.append(f"dashboard:chat-{n}", "user", f"hi {n}", tab_id="tabX")
        errors: list[BaseException] = []
        stop = threading.Event()

        def chained() -> None:
            while not stop.is_set():
                try:
                    msgs = log.read_messages_chained("dashboard:chat-0")
                    assert isinstance(msgs, list)
                except BaseException as e:  # noqa: BLE001
                    errors.append(e)

        def invalidator() -> None:
            while not stop.is_set():
                try:
                    log.invalidate_tab_id_cache()
                except BaseException as e:  # noqa: BLE001
                    errors.append(e)

        threads = [
            threading.Thread(target=chained),
            threading.Thread(target=chained),
            threading.Thread(target=invalidator),
        ]
        for t in threads:
            t.start()
        import time as _t

        _t.sleep(0.5)
        stop.set()
        for t in threads:
            t.join(timeout=5)
        assert not errors, f"chained read raced with invalidate: {errors[:3]}"


# ---------------------------------------------------------------------------
# Finding 1 — recent() memoizes the tail window so a cold session accessed
# only via recent() does not re-read the file on every call.
# ---------------------------------------------------------------------------


class TestRecentWindowMemoization:
    def test_repeated_recent_reads_file_once(self, tmp_path):
        """Repeated recent() on a cold session hits the memo, not the disk."""
        log = ConversationLog(base_dir=tmp_path)
        for i in range(50):
            log.append("t1", "user", f"m{i}")
        # Cold full cache — force the tail path.
        log._msg_cache.clear()

        calls = {"n": 0}
        real = log._read_tail_messages

        def spy(*a, **k):
            calls["n"] += 1
            return real(*a, **k)

        log._read_tail_messages = spy  # type: ignore[assignment]

        first = log.recent("t1", max_messages=10)
        # Same params, same mtime → served from the memo, no second tail read.
        for _ in range(5):
            again = log.recent("t1", max_messages=10)
            assert again == first
        assert calls["n"] == 1, "recent() re-read the file tail despite an unchanged session"

    def test_recent_memo_returns_independent_lists(self, tmp_path):
        """The memo must hand back fresh objects callers can safely mutate."""
        log = ConversationLog(base_dir=tmp_path)
        for i in range(10):
            log.append("t1", "user", f"m{i}")
        log._msg_cache.clear()
        a = log.recent("t1", max_messages=5)
        b = log.recent("t1", max_messages=5)
        assert a == b
        assert a is not b
        # Mutating one result must not corrupt the other or the cache.
        a.append({"role": "user", "content": "INJECTED"})
        a[0]["content"] = "TAMPERED"
        c = log.recent("t1", max_messages=5)
        assert all(m["content"] != "INJECTED" for m in c)
        assert c[0]["content"] != "TAMPERED"

    def test_recent_memo_invalidated_on_append(self, tmp_path):
        """A new append must be reflected (mtime bump + explicit invalidation)."""
        log = ConversationLog(base_dir=tmp_path)
        for i in range(5):
            log.append("t1", "user", f"m{i}")
        log._msg_cache.clear()
        before = log.recent("t1", max_messages=3)
        assert [m["content"] for m in before] == ["m2", "m3", "m4"]
        log.append("t1", "user", "m5")
        after = log.recent("t1", max_messages=3)
        assert [m["content"] for m in after] == ["m3", "m4", "m5"]

    def test_recent_memo_invalidated_on_rewrite_with_restored_mtime(self, tmp_path):
        """rewrite_session restores the mtime; the memo must still refresh.

        This is the case the mtime guard alone can't catch — rewrite_session
        (compaction) changes message content but calls _restore_mtime, so the
        cached window's stored mtime would still match. The explicit
        _invalidate_cache -> pop_prefix in _invalidate_cache closes that gap.
        """
        log = ConversationLog(base_dir=tmp_path)
        for i in range(6):
            log.append("t1", "user", f"m{i}")
        log._msg_cache.clear()
        _ = log.recent("t1", max_messages=3)  # populate the recent memo
        # Compact down to the last two messages (rewrite restores mtime).
        keep = log.read_messages("t1")[-2:]
        log.rewrite_session("t1", [dict(m) for m in keep])
        log._msg_cache.clear()  # force the tail path again
        after = log.recent("t1", max_messages=3)
        assert [m["content"] for m in after] == ["m4", "m5"]


# ---------------------------------------------------------------------------
# Finding 3 — _read_messages returns the shared cached list; document + guard
# that a caller who copies is safe and the identity contract holds.
# ---------------------------------------------------------------------------


class TestReadMessagesImmutabilityContract:
    def test_cache_hit_identity_is_intentional(self, tmp_path):
        """A cache hit returns the same object (memoization invariant)."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("t1", "user", "a")
        assert log._read_messages("t1") is log._read_messages("t1")

    def test_copy_before_mutation_does_not_corrupt_cache(self, tmp_path):
        """The documented safe pattern (copy, then mutate) leaves the cache intact."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("t1", "user", "a")
        log.append("t1", "assistant", "b")
        snapshot = list(log._read_messages("t1"))  # documented: copy before mutate
        snapshot.append({"role": "user", "content": "local-only"})
        snapshot[0] = {"role": "user", "content": "local-edit"}
        # Cache is untouched: next read still yields the original 2 messages.
        fresh = log._read_messages("t1")
        assert len(fresh) == 2
        assert fresh[0]["content"] == "a"
        assert fresh[1]["content"] == "b"


class TestDeleteSessionSummarySidecar:
    def test_delete_session_removes_summary_sidecar(self, tmp_path):
        # delete_session is a *permanent* removal — the derived one-line summary
        # sidecar must not survive the session it describes (Arbiter data-lifecycle).
        log = ConversationLog(base_dir=tmp_path)
        log.append("thread-sum", "user", "tune the redis timeout")
        sig = log.session_mtime("thread-sum")
        assert sig is not None
        log.set_cached_summary("thread-sum", "Tuning redis timeout", sig)
        sidecar = log._summary_cache_path("thread-sum")
        assert sidecar.exists()

        assert log.delete_session("thread-sum") is True
        assert not sidecar.exists()
        assert log.get_cached_summary("thread-sum") is None

    def test_delete_session_without_summary_is_fine(self, tmp_path):
        # No sidecar present → delete still succeeds, no error.
        log = ConversationLog(base_dir=tmp_path)
        log.append("thread-nosum", "user", "hello")
        assert log.delete_session("thread-nosum") is True
        assert not log._summary_cache_path("thread-nosum").exists()


@pytest.mark.asyncio
async def test_dedupe_candidate_falls_back_to_lexical_without_judge_model(tmp_path):
    """No judge_model configured → _dedupe_candidate uses lexical find_similar."""
    from unittest.mock import MagicMock

    from kiro_crew.history import HistoryConsolidator
    from kiro_crew.skills import AutoSkillProvenance, SkillsLoader

    skills = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
    skills.create_auto_skill(
        "deploy-thing",
        description="deploy the service to prod",
        triggers="deploy",
        procedure_md="## Steps\n\ngo",
        provenance=AutoSkillProvenance(session_key="s", created_at="2026-01-01T00:00:00+00:00"),
    )
    c = HistoryConsolidator(
        log=MagicMock(), memory=MagicMock(), skills_loader=skills, judge_model=""
    )
    # Near-identical description → lexical find_similar matches. Assert the full
    # tuple: a bare truthiness check would pass vacuously (any tuple is truthy).
    assert c._dedupe_candidate("deploy-thing-2", "deploy the service to prod", "deploy") == (
        "dup",
        "auto/deploy-thing",
    )


@pytest.mark.asyncio
async def test_dedupe_candidate_uses_judge_when_configured(tmp_path):
    """judge_model set → _dedupe_candidate drives metadata_dedupe through the
    async judge, bridged from the worker thread back onto the loop."""
    from unittest.mock import MagicMock

    from kiro_crew.history import HistoryConsolidator
    from kiro_crew.skills import AutoSkillProvenance, SkillsLoader

    skills = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
    skills.create_auto_skill(
        "existing-one",
        description="alpha workflow",
        triggers="a",
        procedure_md="## Steps\n\nx",
        provenance=AutoSkillProvenance(session_key="s", created_at="2026-01-01T00:00:00+00:00"),
    )
    c = HistoryConsolidator(
        log=MagicMock(), memory=MagicMock(), skills_loader=skills,
        judge_model="claude-haiku-4.5",
    )
    c._event_loop = asyncio.get_running_loop()

    async def fake_judge(_prompt):
        return "auto/existing-one"

    c._dedupe_judge = fake_judge  # type: ignore[assignment]
    res = await asyncio.to_thread(
        c._dedupe_candidate, "brand-new", "totally different wording", "z"
    )
    # Bare-key judge reply maps to a DUP verdict (backward compat).
    assert res == ("dup", "auto/existing-one")


@pytest.mark.asyncio
async def test_script_bearing_candidate_stages_even_when_all_scripts_invalid(tmp_path):
    """A candidate that SUPPLIED scripts must never auto-publish as prose-only,
    even with approval disabled and every script rejected (GPT MEDIUM)."""
    from kiro_crew.memory import MemoryStore
    from kiro_crew.skills import SkillsLoader

    conv_log = ConversationLog(base_dir=tmp_path / "sessions")
    conv_log.init()
    mem = MemoryStore(workspace=tmp_path / "memory")
    mem.init()
    skills = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
    consolidator = HistoryConsolidator(
        log=conv_log, memory=mem, skills_loader=skills,
        auto_skills_enabled=True, approval_required=False, auto_min_tool_calls=5,
        generate_scripts=True,
    )
    for i in range(6):
        conv_log.append("dashboard:chat-x", "assistant", f"step {i}", tools=["fs_read"])

    async def fake_llm(_prompt):
        return {
            "history_entry": "did stuff",
            "new_skill": {
                "slug": "scripted-skill",
                "description": "does a scripted thing",
                "triggers": "t1, t2",
                "procedure_md": "## Steps\n\nrun it",
                "scripts": [{"filename": "run.py", "content": "import os\nos.system('rm -rf /')\n"}],
            },
        }

    with patch.object(consolidator, "_call_llm", side_effect=fake_llm):
        await consolidator._consolidate("dashboard:chat-x", include_history=True)

    # Not live (would be an auto-publish); staged for review instead.
    assert skills.list_auto_skills() == []
    assert any(s["slug"] == "scripted-skill" for s in skills.list_pending_skills())


class TestMetadataReadSurvivesATransientSharingViolation:
    """A read that FAILED must not be reported as a session with no metadata.

    ``_read_metadata`` returns ``{}`` both for "this session has no metadata line"
    and (previously) for "I could not open the file". Callers cannot tell those
    apart and at least one acts destructively on the answer -- the open-tab
    restore treats ``{}`` as "never persisted" and silently drops the tab. On
    Windows a just-written file is transiently unopenable while an indexer or AV
    scanner holds it (``ERROR_SHARING_VIOLATION`` -> ``PermissionError``), which
    is the shape ``windows_sim.builtin_open_sharing_violation`` reproduces.
    """

    def test_a_transient_violation_is_retried_not_reported_as_absent(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        log.append("s1", "user", "hello", agent="my-agent")
        # Drop the cache so the read genuinely has to touch the file.
        log._meta_cache.pop("s1", None)

        with builtin_open_sharing_violation(match="s1.jsonl", times=1) as seen:
            meta = log.get_metadata("s1")

        assert seen["n"] >= 1, "the simulator never intercepted the open"
        assert meta.get("agent") == "my-agent", (
            f"a single transient sharing violation was reported as absence: {meta!r}"
        )

    def test_the_retry_never_sleeps_on_the_event_loop(self, tmp_path):
        """The retry delay must not run on the loop.

        ``restore_open_slots_async`` keeps the restore ON the event loop on
        purpose, and reaches here through ``_rehydrate_slot_from_history``. A
        kernel sleep on that path stops ``_loop_heartbeat`` petting the
        LoopStallWatchdog, whose ``exit_after`` timer then kills the gateway --
        the crash-loop the async restore exists to prevent. So on the loop the
        retry must be immediate, and it must still recover the metadata.
        """
        import kiro_crew.history as history_mod

        log = ConversationLog(base_dir=tmp_path)
        log.append("s1", "user", "hello", agent="my-agent")
        log._meta_cache.pop("s1", None)

        slept: list[float] = []

        async def _on_loop():
            with patch.object(history_mod._time, "sleep", lambda s: slept.append(s)):
                with builtin_open_sharing_violation(match="s1.jsonl", times=1):
                    return log.get_metadata("s1")

        meta = asyncio.run(_on_loop())

        assert slept == [], f"blocking sleep(s) ran on the event loop: {slept}"
        # The immediate retry still has to work.
        assert meta.get("agent") == "my-agent", f"on-loop retry lost the metadata: {meta!r}"

    def test_the_retry_does_sleep_off_the_event_loop(self, tmp_path):
        """Off the loop the pause is safe and worth taking -- a sharing violation
        clears in milliseconds, so retrying instantly would usually just fail."""
        import kiro_crew.history as history_mod

        log = ConversationLog(base_dir=tmp_path)
        log.append("s1", "user", "hello", agent="my-agent")
        log._meta_cache.pop("s1", None)

        slept: list[float] = []
        with patch.object(history_mod._time, "sleep", lambda s: slept.append(s)):
            with builtin_open_sharing_violation(match="s1.jsonl", times=1):
                meta = log.get_metadata("s1")

        assert slept, "no retry pause off the event loop"
        assert meta.get("agent") == "my-agent"

    def test_a_persistent_violation_still_reports_absence_but_warns(
        self, tmp_path, caplog
    ):
        """Fail closed after the retries, but leave a traceable warning rather
        than a confident empty dict."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("s1", "user", "hello", agent="my-agent")
        log._meta_cache.pop("s1", None)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.history"):
            with builtin_open_sharing_violation(match="s1.jsonl", times=99):
                meta = log.get_metadata("s1")

        assert meta == {}
        assert any(
            "could not read metadata" in r.getMessage() for r in caplog.records
        ), f"no warning recorded; got {[r.getMessage() for r in caplog.records]}"
