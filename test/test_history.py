"""Tests for conversation history module.

This file covers the ``ConversationLog`` core: appends, recent windows,
provenance, consolidation counts, rotation and metadata, ``rewrite_session``,
``recent_from_source``, ``sliding_window``, canonical keys,
``has_log``, ``list_sessions`` deduplication and ``agent_usage``. Seven sibling
files hold the rest of this suite: ``test_history_session_search.py`` (content
search and its query parsing), ``test_history_forge_refs.py`` (the spellings of
pull-request, issue and provider references), ``test_history_retention.py``
(archives and session deletion), ``test_history_storage.py`` (the caches, tail
reads, concurrency and the append paths), ``test_history_consolidator.py`` (the
background consolidator's memory and lesson writes),
``test_history_auto_skills.py`` (its skill detection and auto-skills) and
``test_history_restore_preambles.py`` (the notes and preambles a restored
session's context is built with).
"""

from __future__ import annotations

import errno
import json
import os
import threading
from unittest.mock import patch

import pytest

from kiro_crew import history_search
from kiro_crew.atomic_write import atomic_write
from kiro_crew.history import (
    _SESSION_KEEP_LINES,
    _SESSION_MAX_BYTES,
    ConversationLog,
)


@pytest.fixture(autouse=True)
def _fresh_unreadable_warnings(_floor_monkeypatch):
    """Each test starts with no unreadable session file already warned about."""
    _floor_monkeypatch.setattr(history_search, "_unreadable_session_files_warned", set())


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

    def test_provenance_never_cites_a_notice(self, tmp_path):
        # A notice is display-only; citing it would hand the model text that
        # only reaches it screened and fenced (the Slack thread-parent row).
        log = ConversationLog(base_dir=tmp_path)
        log.append("t1", "notice", "Thread started by A on Slack:\nparent", source_thread="t1")
        log.append("t1", "user", "reply", source_thread="t1")
        assert [p["snippet"] for p in log.recent_with_provenance("t1")] == ["reply"]

    def test_unconsolidated_count(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        for i in range(10):
            log.append("t1", "user", f"msg {i}")
        assert log.unconsolidated_count("t1") == 10

    def test_uncached_count_matches_and_leaves_the_cache_empty(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        for i in range(6):
            log.append("t1", "user", f"msg {i} with a\u2028separator")
        log.mark_consolidated("t1", 2)
        with open(log._path("t1"), "a", encoding="utf-8") as fh:
            fh.write("not json\n[1, 2]\n\n")
        log._msg_cache.clear()
        assert log.unconsolidated_count("t1") == 4
        assert "t1" not in log._msg_cache
        assert len(log._read_messages("t1")) - 2 == 4  # the cached path agrees
        assert "t1" in log._msg_cache
        assert log.unconsolidated_count("t1") == 4  # and a hit is reused
        assert log.unconsolidated_count("missing") == 0

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

    def test_caches_refresh_after_mtime_preserving_external_rewrite(self, tmp_path):
        writer = ConversationLog(base_dir=tmp_path)
        writer.append("t1", "user", "first")
        reader = ConversationLog(base_dir=tmp_path)
        assert reader.get_metadata("t1").get("last_consolidated", 0) == 0
        assert [message["content"] for message in reader._read_messages("t1")] == ["first"]
        assert [message["content"] for message in reader.recent("t1")] == ["first"]

        path = writer._path("t1")
        before = path.stat()
        rows = path.read_text(encoding="utf-8").splitlines()
        metadata = json.loads(rows[0])
        metadata["last_consolidated"] = 1
        atomic_write(
            path,
            "\n".join(
                [
                    json.dumps(metadata),
                    *rows[1:],
                    json.dumps({"role": "user", "content": "second"}),
                ]
            )
            + "\n",
        )
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))

        assert path.stat().st_mtime_ns == before.st_mtime_ns
        assert reader.get_metadata("t1")["last_consolidated"] == 1
        assert [message["content"] for message in reader._read_messages("t1")] == [
            "first",
            "second",
        ]
        assert [message["content"] for message in reader.recent("t1")] == ["first", "second"]

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
        # Rotation needs BOTH gates crossed: more than ``_SESSION_KEEP_LINES``
        # lines and more than ``_SESSION_MAX_BYTES`` bytes. Derive the row size
        # from the budget instead of hardcoding a byte total, so raising the cap
        # cannot leave this test green while not reaching rotation at all.
        rows = _SESSION_KEEP_LINES + 50
        content = "x" * (_SESSION_MAX_BYTES // rows + 1024)
        for i in range(rows):
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
        # Row size derived from the budget for the same reason as
        # ``test_rotation``: the assertion below only means anything if the
        # second loop actually re-crosses the byte cap after
        # ``mark_consolidated``, at whatever the cap is set to.
        rows = _SESSION_KEEP_LINES + 50
        content = "x" * (_SESSION_MAX_BYTES // rows + 1024)
        for i in range(rows):
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

        A ``!ta <agent> --clean`` issued before the first message is logged
        would be silently dropped (the file did not exist yet), so the
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

    def test_list_sessions_status_reports_scandir_failure(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)

        with patch.object(history_search.os, "scandir", side_effect=OSError("disk gone")):
            assert log.list_sessions_status() == ([], False)
            assert log.list_sessions() == []

    def test_list_sessions_status_treats_missing_directory_as_complete(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path / "missing")

        assert log.list_sessions_status() == ([], True)

    @pytest.mark.parametrize(
        ("error", "expected_complete"),
        [
            (FileNotFoundError(errno.ENOENT, "gone"), True),
            (OSError(errno.EIO, "read failed"), False),
        ],
    )
    def test_list_sessions_status_distinguishes_vanished_and_unreadable_entries(
        self, tmp_path, error, expected_complete
    ):
        log = ConversationLog(base_dir=tmp_path)
        log.append("visible", "user", "positive control")
        assert log.list_sessions_status()[1] is True

        broken_path = os.fspath(tmp_path / "broken.jsonl")

        class _Entry:
            name = "broken.jsonl"
            path = broken_path

            @staticmethod
            def stat():
                raise error

        class _Scan:
            def __enter__(self):
                return iter([_Entry()])

            def __exit__(self, *_args):
                return None

        real_os_stat = os.stat

        def _os_stat(path, *args, **kwargs):
            if os.fspath(path) == broken_path:
                raise error
            return real_os_stat(path, *args, **kwargs)

        with (
            patch.object(history_search.os, "scandir", return_value=_Scan()),
            patch.object(history_search.os, "stat", _os_stat),
        ):
            sessions, complete = log.list_sessions_status()

        assert sessions == []
        assert complete is expected_complete

    def test_unreadable_file_check_and_add_run_under_one_lock(self, monkeypatch):
        """Listings run on executor threads, so the warned-set check and its add
        must both happen under the lock or two racing callers both log."""
        lock = threading.Lock()
        seen_unlocked: list[str] = []

        class _LockCheckingSet(set):
            def __contains__(self, item):
                if not lock.locked():
                    seen_unlocked.append("check")
                return super().__contains__(item)

            def add(self, item):
                if not lock.locked():
                    seen_unlocked.append("add")
                super().add(item)

        monkeypatch.setattr(history_search, "_unreadable_session_files_lock", lock)
        monkeypatch.setattr(history_search, "_unreadable_session_files_warned", _LockCheckingSet())

        history_search._warn_unreadable_session_file(
            "/sessions/broken.jsonl", PermissionError(errno.EACCES, "denied")
        )

        assert seen_unlocked == []
        assert set(history_search._unreadable_session_files_warned) == {"/sessions/broken.jsonl"}

    def test_list_sessions_status_warns_once_per_unreadable_file(
        self, tmp_path, caplog, monkeypatch
    ):
        """One unreadable file holds every unread dot for the process lifetime,
        so it is warned about, but once: the listing runs every few seconds."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("visible", "user", "positive control")
        broken_path = os.fspath(tmp_path / "broken.jsonl")
        (tmp_path / "broken.jsonl").write_text("")
        monkeypatch.setattr(
            history_search, "_unreadable_session_files_warned", set(), raising=False
        )

        real_os_stat = os.stat

        def _os_stat(path, *args, **kwargs):
            if os.fspath(path) == broken_path:
                raise PermissionError(errno.EACCES, "denied", broken_path)
            return real_os_stat(path, *args, **kwargs)

        class _Entry:
            def __init__(self, real):
                self._real = real
                self.name = real.name
                self.path = real.path

            def stat(self):
                return _os_stat(self.path)

            def is_symlink(self):
                return self._real.is_symlink()

        real_scandir = os.scandir

        class _Scan:
            def __init__(self, base):
                self._it = real_scandir(base)

            def __enter__(self):
                return (_Entry(e) for e in self._it.__enter__())

            def __exit__(self, *args):
                return self._it.__exit__(*args)

        with (
            patch.object(history_search.os, "scandir", _Scan),
            patch.object(history_search.os, "stat", _os_stat),
            caplog.at_level("WARNING", logger=history_search.logger.name),
        ):
            for _ in range(2):
                sessions, complete = log.list_sessions_status()
                assert [s["key"] for s in sessions] == ["visible"]
                assert complete is False

        warnings = [r for r in caplog.records if broken_path in r.getMessage()]
        assert len(warnings) == 1
        assert warnings[0].levelname == "WARNING"

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
        against.
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
