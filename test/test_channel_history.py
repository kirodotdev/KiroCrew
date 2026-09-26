"""Tests for channel history buffer."""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
import stat
import threading
import time

import pytest

from kiro_crew import channel_history, platform_compat
from kiro_crew.channel_history import ChannelHistory


def _drain_lane() -> None:
    """Block until every job already queued on the history disk lane ran.

    All observe-file IO — appends and terminal rewrites/unlinks alike —
    executes on the single-worker lane regardless of the calling thread, so
    a sync test must drain the lane before asserting on file contents.
    """
    from kiro_crew.executors import channel_history_executor

    channel_history_executor().submit(lambda: None).result(timeout=10)


@pytest.fixture(autouse=True)
def _no_queued_write_outlives_the_test(tmp_path):
    """Drain the lane at teardown, before ``tmp_path`` is torn down.

    Module-wide on purpose: every test that touches ``ChannelHistory`` can
    leave writes queued on the process-wide lane — including a test whose
    assertion fails BEFORE its own drain — and a queued closure running after
    ``tmp_path`` teardown would recreate the removed directory. Depending on
    ``tmp_path`` orders this fixture's teardown ahead of the directory's.
    """
    yield
    _drain_lane()


class TestChannelHistory:
    """Tests for the rolling-window channel history buffer."""

    def test_push_and_context(self):
        """Push messages, get formatted context."""
        h = ChannelHistory()
        h.push("C123", "alice", "The pipeline is broken")
        h.push("C123", "bob", "I see 5xx errors in us-west-2")

        ctx = h.context_for("C123")
        assert "[Recent channel messages for context:]" in ctx
        assert "alice" in ctx
        assert "pipeline is broken" in ctx
        assert "bob" in ctx
        assert "5xx errors" in ctx
        assert "[End of channel context]" in ctx

    def test_empty_channel_returns_empty(self):
        """No messages → empty string."""
        h = ChannelHistory()
        assert h.context_for("C999") == ""

    def test_per_channel_isolation(self):
        """Different channels have independent buffers."""
        h = ChannelHistory()
        h.push("C1", "alice", "topic A")
        h.push("C2", "bob", "topic B")

        ctx1 = h.context_for("C1")
        ctx2 = h.context_for("C2")
        assert "topic A" in ctx1
        assert "topic B" not in ctx1
        assert "topic B" in ctx2
        assert "topic A" not in ctx2

    def test_max_entries(self):
        """Buffer respects max_entries (oldest evicted)."""
        h = ChannelHistory(max_entries=3)
        h.push("C1", "a", "msg1")
        h.push("C1", "b", "msg2")
        h.push("C1", "c", "msg3")
        h.push("C1", "d", "msg4")  # should evict msg1

        ctx = h.context_for("C1")
        assert "msg1" not in ctx
        assert "msg4" in ctx
        assert h.entry_count("C1") == 3

    def test_ttl_expiry(self):
        """Old entries are evicted based on TTL."""
        h = ChannelHistory(ttl_secs=1)
        h.push("C1", "alice", "old message")

        # Fake the timestamp to be in the past
        h._channels["C1"][0].timestamp = time.monotonic() - 5

        h.push("C1", "bob", "new message")

        ctx = h.context_for("C1")
        assert "old message" not in ctx
        assert "new message" in ctx
        assert h.entry_count("C1") == 1

    def test_clear_channel(self):
        """clear() removes all entries for a channel."""
        h = ChannelHistory()
        h.push("C1", "alice", "hello")
        h.push("C1", "bob", "world")
        assert h.entry_count("C1") == 2

        h.clear("C1")
        assert h.entry_count("C1") == 0
        assert h.context_for("C1") == ""

    def test_channel_count(self):
        """channel_count tracks number of channels with history."""
        h = ChannelHistory()
        assert h.channel_count == 0
        h.push("C1", "a", "x")
        h.push("C2", "b", "y")
        assert h.channel_count == 2

    def test_empty_text_ignored(self):
        """Push with empty text is a no-op."""
        h = ChannelHistory()
        h.push("C1", "alice", "")
        assert h.entry_count("C1") == 0

    def test_empty_channel_ignored(self):
        """Push with empty channel is a no-op."""
        h = ChannelHistory()
        h.push("", "alice", "hello")
        assert h.channel_count == 0

    def test_long_message_truncated_in_context(self):
        """Very long messages are truncated to 300 chars in context output."""
        h = ChannelHistory()
        long_msg = "x" * 500
        h.push("C1", "alice", long_msg)

        ctx = h.context_for("C1")
        # Should contain truncated version (300 chars + …)
        assert "…" in ctx
        assert "x" * 301 not in ctx

    def test_age_formatting(self):
        """Age strings show seconds for recent, minutes for older."""
        h = ChannelHistory()
        h.push("C1", "alice", "just now")
        # Recent message should show "Xs ago"
        ctx = h.context_for("C1")
        assert "s ago" in ctx


class TestThreadIsolation:
    """Thread context isolation — no cross-thread leakage."""

    def test_thread_only_sees_own_messages(self):
        """context_for with thread_ts only returns that thread's messages."""
        h = ChannelHistory()
        h.push("C1", "alice", "thread A msg", thread_ts="T1")
        h.push("C1", "bob", "thread B msg", thread_ts="T2")
        h.push("C1", "carol", "top-level msg")

        ctx = h.context_for("C1", thread_ts="T1")
        assert "thread A msg" in ctx
        assert "thread B msg" not in ctx
        assert "top-level msg" not in ctx

    def test_top_level_excludes_threaded_messages(self):
        """context_for without thread_ts excludes all threaded messages."""
        h = ChannelHistory()
        h.push("C1", "alice", "thread msg", thread_ts="T1")
        h.push("C1", "bob", "channel msg")

        ctx = h.context_for("C1")
        assert "channel msg" in ctx
        assert "thread msg" not in ctx

    def test_empty_thread_returns_empty(self):
        """Thread with no matching messages returns empty string."""
        h = ChannelHistory()
        h.push("C1", "alice", "other thread", thread_ts="T1")

        assert h.context_for("C1", thread_ts="T999") == ""

    def test_thread_context_format(self):
        """Thread context uses [Current thread:] header, no [Other threads:]."""
        h = ChannelHistory()
        h.push("C1", "alice", "hello", thread_ts="T1")

        ctx = h.context_for("C1", thread_ts="T1")
        assert "[Current thread:]" in ctx
        assert "[Other threads:]" not in ctx
        assert "[End of channel context]" in ctx


class TestObservePersistence:
    """Tests for observe-mode JSONL disk persistence."""

    def test_observe_push_writes_jsonl(self, tmp_path):
        """Pushing to an observe channel appends a JSONL line to disk."""
        h = ChannelHistory(history_dir=tmp_path)
        h.set_observe("C1")
        h.push("C1", "alice", "hello world")
        _drain_lane()

        path = tmp_path / "C1.jsonl"
        assert path.exists()
        lines = path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["user"] == "alice"
        assert data["text"] == "hello world"
        assert data["ts"] is not None

    def test_oversized_text_is_bounded_in_memory_and_on_disk(self, tmp_path):
        """Externally controlled text is bounded before deque and JSONL storage."""
        h = ChannelHistory(history_dir=tmp_path)
        h.set_observe("C1")
        oversized = "x" * (channel_history.HISTORY_MAX_TEXT_CHARS + 1)
        expected = oversized[: channel_history.HISTORY_MAX_TEXT_CHARS]

        h.push("C1", "alice", oversized)
        _drain_lane()

        assert h._channels["C1"][0].text == expected
        data = json.loads((tmp_path / "C1.jsonl").read_text(encoding="utf-8").strip())
        assert data["text"] == expected

    def test_non_observe_no_disk_io(self, tmp_path):
        """Non-observe channels do not write to disk."""
        h = ChannelHistory(history_dir=tmp_path)
        h.push("C1", "alice", "hello")

        assert not (tmp_path / "C1.jsonl").exists()

    def test_load_observe_restores_history(self, tmp_path):
        """set_observe loads persisted history from disk."""
        # Write some history to disk
        path = tmp_path / "C1.jsonl"
        now = time.time()
        entries = [
            {"user": "alice", "text": "msg1", "thread_ts": None, "ts": now - 60},
            {"user": "bob", "text": "msg2", "thread_ts": None, "ts": now - 30},
        ]
        with path.open("w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

        h = ChannelHistory(history_dir=tmp_path)
        h.set_observe("C1")

        assert h.entry_count("C1") == 2
        ctx = h.context_for("C1")
        assert "msg1" in ctx
        assert "msg2" in ctx

    def test_load_observe_keeps_newest_entries_and_rewrites_file(self, tmp_path, caplog):
        """An oversized legacy file is capped during load and compacted on disk."""
        path = tmp_path / "C1.jsonl"
        now = time.time()
        entries = [
            {"user": "alice", "text": f"msg{index}", "msg_ts": str(index), "ts": now}
            for index in range(5)
        ]
        path.write_text(
            "".join(json.dumps(entry) + "\n" for entry in entries),
            encoding="utf-8",
        )

        h = ChannelHistory(history_dir=tmp_path, observe_max_entries=3)
        with caplog.at_level(logging.INFO, logger=channel_history.__name__):
            h.set_observe("C1")
        _drain_lane()

        assert [entry.text for entry in h._channels["C1"]] == ["msg2", "msg3", "msg4"]
        persisted = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        assert [entry["text"] for entry in persisted] == ["msg2", "msg3", "msg4"]
        assert "dropped 2 over cap" in caplog.text

    def test_load_observe_bounds_fields_and_rewrites_file(self, tmp_path):
        """Legacy fields are bounded in memory and normalized on disk."""
        path = tmp_path / "C1.jsonl"
        oversized_user = "u" * (channel_history.HISTORY_MAX_ID_CHARS + 1)
        oversized_text = "x" * (channel_history.HISTORY_MAX_TEXT_CHARS + 1)
        oversized_thread_ts = "t" * (channel_history.HISTORY_MAX_ID_CHARS + 1)
        oversized_msg_ts = "m" * (channel_history.HISTORY_MAX_ID_CHARS + 1)
        path.write_text(
            json.dumps(
                {
                    "user": oversized_user,
                    "text": oversized_text,
                    "thread_ts": oversized_thread_ts,
                    "msg_ts": oversized_msg_ts,
                    "ts": time.time(),
                }
            )
            + "\n",
            encoding="utf-8",
        )

        h = ChannelHistory(history_dir=tmp_path)
        h.set_observe("C1")
        _drain_lane()

        entry = h._channels["C1"][0]
        assert entry.user == oversized_user[: channel_history.HISTORY_MAX_ID_CHARS]
        assert entry.text == oversized_text[: channel_history.HISTORY_MAX_TEXT_CHARS]
        assert entry.thread_ts == oversized_thread_ts[: channel_history.HISTORY_MAX_ID_CHARS]
        assert entry.msg_ts == oversized_msg_ts[: channel_history.HISTORY_MAX_ID_CHARS]
        persisted = json.loads(path.read_text(encoding="utf-8"))
        assert persisted["user"] == entry.user
        assert persisted["text"] == entry.text
        assert persisted["thread_ts"] == entry.thread_ts
        assert persisted["msg_ts"] == entry.msg_ts

    def test_load_observe_filters_expired(self, tmp_path):
        """Expired entries are filtered out on load."""
        path = tmp_path / "C1.jsonl"
        now = time.time()
        entries = [
            {"user": "alice", "text": "old", "thread_ts": None, "ts": now - 700000},  # expired
            {"user": "bob", "text": "recent", "thread_ts": None, "ts": now - 10},
        ]
        with path.open("w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

        h = ChannelHistory(history_dir=tmp_path)
        h.set_observe("C1")

        assert h.entry_count("C1") == 1
        ctx = h.context_for("C1")
        assert "old" not in ctx
        assert "recent" in ctx

    def test_lazy_compaction_rewrites_file(self, tmp_path):
        """Loading with expired entries compacts the file."""
        path = tmp_path / "C1.jsonl"
        now = time.time()
        entries = [
            {"user": "alice", "text": "old", "thread_ts": None, "ts": now - 700000},
            {"user": "bob", "text": "recent", "thread_ts": None, "ts": now - 10},
        ]
        with path.open("w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

        h = ChannelHistory(history_dir=tmp_path)
        h.set_observe("C1")
        _drain_lane()

        # File should be compacted — only the recent entry remains
        lines = path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["user"] == "bob"

    def test_invalid_record_shapes_are_skipped(self, tmp_path, caplog):
        """Valid JSON with unsafe field shapes cannot crash observe startup."""
        path = tmp_path / "C1.jsonl"
        now = time.time()
        records = [
            {"user": "bob", "text": "good", "msg_ts": "1", "ts": now},
            {"user": "mallory", "text": "bad", "msg_ts": [1], "ts": now},
            ["not", "an", "object"],
        ]
        path.write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )

        h = ChannelHistory(history_dir=tmp_path)
        h.set_observe("C1")

        assert h.entry_count("C1") == 1
        assert "good" in h.context_for("C1")
        warnings = [
            record.getMessage()
            for record in caplog.records
            if "Invalid history record" in record.getMessage()
        ]
        assert len(warnings) == 2
        assert all("C1" in warning for warning in warnings)

    def test_invalid_wall_timestamps_are_skipped(self, tmp_path, caplog):
        """Non-finite and unrepresentable timestamps cannot crash startup."""
        path = tmp_path / "C1.jsonl"
        records = [
            {"user": "alice", "text": "nan", "ts": float("nan")},
            {"user": "alice", "text": "infinity", "ts": float("inf")},
            {"user": "alice", "text": "huge", "ts": 10**400},
            {"user": "bob", "text": "good", "ts": time.time()},
        ]
        path.write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )

        h = ChannelHistory(history_dir=tmp_path)
        h.set_observe("C1")

        assert [entry.text for entry in h._channels["C1"]] == ["good"]
        warnings = [
            record.getMessage()
            for record in caplog.records
            if "Invalid history record" in record.getMessage()
        ]
        assert len(warnings) == 3
        assert all("C1" in warning for warning in warnings)

    def test_corrupt_lines_skipped(self, tmp_path):
        """Corrupt JSONL lines are skipped gracefully."""
        path = tmp_path / "C1.jsonl"
        now = time.time()
        with path.open("w") as f:
            f.write("not valid json\n")
            f.write(
                json.dumps({"user": "bob", "text": "good", "thread_ts": None, "ts": now}) + "\n"
            )

        h = ChannelHistory(history_dir=tmp_path)
        h.set_observe("C1")

        assert h.entry_count("C1") == 1
        ctx = h.context_for("C1")
        assert "good" in ctx

    def test_unset_observe_removes_file(self, tmp_path):
        """unset_observe deletes the JSONL file."""
        h = ChannelHistory(history_dir=tmp_path)
        h.set_observe("C1")
        h.push("C1", "alice", "msg")
        _drain_lane()

        path = tmp_path / "C1.jsonl"
        assert path.exists()

        h.unset_observe("C1")
        _drain_lane()
        assert not path.exists()

    def test_thread_ts_persisted(self, tmp_path):
        """thread_ts is round-tripped through JSONL."""
        h = ChannelHistory(history_dir=tmp_path)
        h.set_observe("C1")
        h.push("C1", "alice", "reply", thread_ts="T1")
        _drain_lane()

        path = tmp_path / "C1.jsonl"
        data = json.loads(path.read_text(encoding="utf-8").strip())
        assert data["thread_ts"] == "T1"

        # Reload and verify
        h2 = ChannelHistory(history_dir=tmp_path)
        h2.set_observe("C1")
        ctx = h2.context_for("C1", thread_ts="T1")
        assert "reply" in ctx

    def test_no_history_dir_graceful(self):
        """With no history_dir, observe mode works in-memory only."""
        h = ChannelHistory(history_dir=None)
        h.set_observe("C1")
        h.push("C1", "alice", "msg")
        assert h.entry_count("C1") == 1

    def test_observe_wall_ts_eviction(self, tmp_path):
        """Observe entries are evicted based on wall clock, not monotonic."""
        h = ChannelHistory(observe_ttl_secs=60, history_dir=tmp_path)
        h.set_observe("C1")
        h.push("C1", "alice", "msg")

        # Fake the wall_ts to be in the past
        h._channels["C1"][0].wall_ts = time.time() - 120

        h.push("C1", "bob", "new")
        assert h.entry_count("C1") == 1
        ctx = h.context_for("C1")
        assert "msg" not in ctx
        assert "new" in ctx


class TestChannelHistoryContext:
    """Integration tests for ContextBuilder + ChannelHistory."""

    # Every test here asserts the SHAPE of a built turn, so the host's own free
    # memory must not be an input: see the fixture for the advisory it pins off.
    pytestmark = pytest.mark.usefixtures("ample_host_resources")

    def test_context_builder_injects_channel_history(self):
        """ContextBuilder includes channel history when channel_id is provided."""
        from kiro_crew.context import ContextBuilder

        h = ChannelHistory()
        h.push("C123", "alice", "pipeline broke")
        h.push("C123", "bob", "checking us-west-2")

        builder = ContextBuilder(channel_history=h)
        msg, _ = builder.build_message("what's going on?", False, channel_id="C123")

        assert "pipeline broke" in msg
        assert "checking us-west-2" in msg
        assert "what's going on?" in msg

    def test_context_builder_no_injection_without_channel_id(self):
        """ContextBuilder does NOT inject channel history for DMs (no channel_id)."""
        from kiro_crew.context import ContextBuilder

        h = ChannelHistory()
        h.push("C123", "alice", "secret channel message")

        builder = ContextBuilder(channel_history=h)
        msg, _ = builder.build_message("hello", False)

        assert "secret channel message" not in msg

    def test_context_builder_no_injection_without_history(self):
        """ContextBuilder works fine with no channel_history set."""
        from kiro_crew.context import ContextBuilder

        builder = ContextBuilder()
        msg, _ = builder.build_message("hello", False, channel_id="C123")

        assert msg.startswith("hello")


class TestObserveFileBounding:
    """The persisted observe file must honour ``observe_max_entries``.

    The in-memory deque is created with ``maxlen=observe_max_entries``, and
    ``_load_observe`` appends every line it reads into that deque — so any
    record past the newest ``observe_max_entries`` is parsed and then
    immediately evicted. Those lines can never reach a caller; they only cost
    disk and a slower load.
    """

    @staticmethod
    def _lines(tmp_path, channel="C1"):
        return (tmp_path / f"{channel}.jsonl").read_text(encoding="utf-8").strip().splitlines()

    @pytest.mark.skipif(not platform_compat.IS_POSIX, reason="POSIX permission bits")
    def test_compaction_rewrite_keeps_observe_file_owner_only(self, tmp_path, monkeypatch):
        """Count-triggered rewrites retain the append path's owner-only mode."""
        real_atomic_write = channel_history.atomic_write

        def _atomic_write_with_wide_default(path, content, *args, **kwargs):
            kwargs.setdefault("mode", 0o644)
            return real_atomic_write(path, content, *args, **kwargs)

        monkeypatch.setattr(channel_history, "atomic_write", _atomic_write_with_wide_default)
        h = ChannelHistory(observe_max_entries=2, history_dir=tmp_path)
        h.set_observe("C1")
        h.push("C1", "alice", "msg0")
        h.push("C1", "alice", "msg1")
        _drain_lane()

        path = tmp_path / "C1.jsonl"
        assert path.stat().st_mode & 0o777 == 0o600

    @pytest.mark.skipif(not platform_compat.IS_POSIX, reason="POSIX permission bits")
    def test_append_tightens_existing_observe_file_to_owner_only(self, tmp_path):
        """Appending tightens a legacy file through its pinned descriptor."""
        path = tmp_path / "C1.jsonl"
        path.write_text(
            json.dumps({"user": "alice", "text": "persisted", "msg_ts": "1", "ts": time.time()})
            + "\n",
            encoding="utf-8",
        )
        path.chmod(0o644)

        h = ChannelHistory(observe_max_entries=2, history_dir=tmp_path)
        h.set_observe("C1")
        h.push("C1", "bob", "appended", msg_ts="2")
        _drain_lane()

        assert path.stat().st_mode & 0o777 == 0o600

    @pytest.mark.skipif(not platform_compat.IS_POSIX, reason="POSIX permission bits")
    @pytest.mark.skipif(not channel_history._SUPPORTS_DIR_FD, reason="dir_fd append path")
    def test_append_lands_when_owner_only_tightening_is_refused(self, tmp_path, monkeypatch):
        """A mount that refuses the owner-only tightening still gets the append.

        The tightening is best effort (``platform_compat.fchmod_safe``): a
        refused ``fchmod`` costs the owner-only guarantee on that mount, never
        the record. Were the refusal to escape past ``f.write``, per-message
        persistence would silently degrade to one rewrite per cap.
        """
        path = tmp_path / "C1.jsonl"
        path.write_text(
            json.dumps({"user": "alice", "text": "persisted", "msg_ts": "1", "ts": time.time()})
            + "\n",
            encoding="utf-8",
        )
        path.chmod(0o644)
        target = os.stat(path)

        real_fchmod = os.fchmod

        def _refuse_for_history_file(fd, mode, *args, **kwargs):
            st = os.fstat(fd)
            if (st.st_dev, st.st_ino) == (target.st_dev, target.st_ino):
                raise PermissionError(errno.EPERM, "Operation not permitted")
            return real_fchmod(fd, mode, *args, **kwargs)

        # Patched on the ``os`` module itself so both the append's call and
        # ``platform_compat.fchmod_safe``'s ``os.fchmod`` see the refusal.
        with monkeypatch.context() as patch:
            patch.setattr(os, "fchmod", _refuse_for_history_file)
            h = ChannelHistory(observe_max_entries=2, history_dir=tmp_path)
            h.set_observe("C1")
            h.push("C1", "bob", "appended", msg_ts="2")
            _drain_lane()

        assert os.fchmod is real_fchmod
        texts = [json.loads(ln)["text"] for ln in self._lines(tmp_path)]
        assert "appended" in texts, f"append skipped after a refused fchmod: {texts}"
        # The mode could not be tightened; the file keeps what the mount allowed.
        assert path.stat().st_mode & 0o777 == 0o644

    def test_appending_past_the_cap_does_not_grow_the_file_without_bound(self, tmp_path):
        """Compaction runs on the write path, not only at the next set_observe."""
        h = ChannelHistory(observe_max_entries=5, history_dir=tmp_path)
        h.set_observe("C1")
        for i in range(20):
            h.push("C1", "alice", f"msg{i}")
        _drain_lane()

        lines = self._lines(tmp_path)
        assert len(lines) <= 10, f"observe file grew to {len(lines)} lines for a cap of 5"
        # Only the reachable window survives — the deque would have evicted the rest.
        assert [json.loads(ln)["text"] for ln in lines][-5:] == [f"msg{i}" for i in range(15, 20)]

    def test_file_stays_within_two_compaction_windows_at_each_drain(self, tmp_path):
        """Each cap's worth of appends rewrites before a third window can form."""
        cap = 3
        h = ChannelHistory(observe_max_entries=cap, history_dir=tmp_path)
        h.set_observe("C1")
        compaction_interval = h._observe_max_entries
        file_bound = h._observe_max_entries + compaction_interval
        assert file_bound == 2 * cap

        for i in range(cap * 4):
            h.push("C1", "alice", f"msg{i}")
            _drain_lane()
            line_count = len(self._lines(tmp_path))
            assert (
                line_count <= file_bound
            ), f"observe file grew to {line_count} lines; bound is {file_bound}"

    def test_append_admission_semaphore_has_documented_64_slots(self, tmp_path):
        """The append queue's semaphore and declared cap stay at 64 slots."""
        h = ChannelHistory(history_dir=tmp_path)

        assert channel_history._DISK_LANE_MAX_PENDING == 64
        assert h._disk_lane_slots._value == channel_history._DISK_LANE_MAX_PENDING

    def test_the_reachable_window_is_intact_after_compaction(self, tmp_path):
        """Bounding the file must not cost the entries the deque still holds."""
        h = ChannelHistory(observe_max_entries=5, history_dir=tmp_path)
        h.set_observe("C1")
        for i in range(20):
            h.push("C1", "alice", f"msg{i}")

        ctx = h.context_for("C1")
        for i in range(15, 20):
            assert f"msg{i}" in ctx
        assert h.entry_count("C1") == 5

        # And it survives a restart that reloads from the compacted file.
        _drain_lane()
        h2 = ChannelHistory(observe_max_entries=5, history_dir=tmp_path)
        h2.set_observe("C1")
        assert h2.entry_count("C1") == 5
        ctx2 = h2.context_for("C1")
        for i in range(15, 20):
            assert f"msg{i}" in ctx2

    def test_an_oversized_inherited_file_is_bounded_on_load(self, tmp_path):
        """A file written by a build that never bounded it must not stay oversized.

        Nothing here is expired, so the existing TTL-triggered lazy compaction
        does not fire.
        """
        path = tmp_path / "C1.jsonl"
        now = time.time()
        with path.open("w", encoding="utf-8") as f:
            for i in range(40):
                f.write(
                    json.dumps(
                        {"user": "alice", "text": f"msg{i}", "thread_ts": None, "ts": now - 1}
                    )
                    + "\n"
                )

        h = ChannelHistory(observe_max_entries=5, history_dir=tmp_path)
        h.set_observe("C1")
        _drain_lane()

        assert len(self._lines(tmp_path)) == 5, "oversized inherited file was not bounded on load"
        assert h.entry_count("C1") == 5

    def test_invalid_parseable_records_are_removed_on_load(self, tmp_path):
        """Invalid JSON records make a quiet inherited file compact itself."""
        cap = 2
        now = time.time()
        path = tmp_path / "C1.jsonl"
        invalid_records = [
            {"user": index, "text": f"invalid{index}", "msg_ts": str(index), "ts": now}
            for index in range(2 * cap + 1)
        ]
        valid_records = [
            {"user": "alice", "text": f"valid{index}", "msg_ts": f"v{index}", "ts": now}
            for index in range(cap)
        ]
        path.write_text(
            "".join(json.dumps(record) + "\n" for record in (*invalid_records, *valid_records)),
            encoding="utf-8",
        )

        history = ChannelHistory(observe_max_entries=cap, history_dir=tmp_path)
        history.set_observe("C1")
        assert [entry.text for entry in history._channels["C1"]] == ["valid0", "valid1"]
        _drain_lane()

        persisted = [json.loads(line) for line in self._lines(tmp_path)]
        assert [record["text"] for record in persisted] == ["valid0", "valid1"]

    def test_blank_lines_are_removed_on_load_without_dropping_valid_records(self, tmp_path):
        """Blank padding makes a quiet inherited file compact itself."""
        cap = 4
        now = time.time()
        path = tmp_path / "C1.jsonl"
        valid_records = [
            {"user": "alice", "text": f"valid{index}", "msg_ts": str(index), "ts": now}
            for index in range(cap)
        ]
        path.write_text(
            "\n \t\n".join(json.dumps(record) for record in valid_records) + "\n\n",
            encoding="utf-8",
        )

        history = ChannelHistory(observe_max_entries=cap, history_dir=tmp_path)
        history.set_observe("C1")
        _drain_lane()

        lines = path.read_text(encoding="utf-8").splitlines()
        assert all(line.strip() for line in lines)
        assert [json.loads(line)["text"] for line in lines] == [
            "valid0",
            "valid1",
            "valid2",
            "valid3",
        ]

    def test_ts_less_line_is_removed_on_load_without_dropping_valid_records(self, tmp_path):
        """A ts-less disk record makes a quiet inherited file compact itself."""
        cap = 3
        now = time.time()
        path = tmp_path / "C1.jsonl"
        valid_records = [
            {"user": "alice", "text": f"valid{index}", "msg_ts": str(index), "ts": now}
            for index in range(cap)
        ]
        ts_less = {
            "user": "u",
            "text": "t",
            "thread_ts": None,
            "msg_ts": None,
            "ts": None,
        }
        path.write_text(
            "".join(
                json.dumps(record) + "\n"
                for record in (valid_records[0], ts_less, *valid_records[1:])
            ),
            encoding="utf-8",
        )

        history = ChannelHistory(observe_max_entries=cap, history_dir=tmp_path)
        history.set_observe("C1")
        _drain_lane()

        persisted = [json.loads(line) for line in self._lines(tmp_path)]
        assert [record["text"] for record in persisted] == ["valid0", "valid1", "valid2"]
        assert all(record["ts"] is not None for record in persisted)

    def test_complete_file_under_cap_is_not_rewritten_on_load(self, tmp_path, monkeypatch):
        """A clean inherited file below the cap does not churn on load."""
        cap = 4
        now = time.time()
        path = tmp_path / "C1.jsonl"
        original = "".join(
            json.dumps({"user": "alice", "text": f"valid{index}", "msg_ts": str(index), "ts": now})
            + "\n"
            for index in range(cap)
        )
        path.write_text(original, encoding="utf-8")
        writes = []
        real_atomic_write = channel_history.atomic_write

        def _recording_atomic_write(*args, **kwargs):
            writes.append((args, kwargs))
            return real_atomic_write(*args, **kwargs)

        monkeypatch.setattr(channel_history, "atomic_write", _recording_atomic_write)
        history = ChannelHistory(observe_max_entries=cap, history_dir=tmp_path)
        history.set_observe("C1")
        _drain_lane()

        assert writes == []
        assert path.read_text(encoding="utf-8") == original

    def test_ts_less_disk_line_rewrite_keeps_the_persistable_mixed_window(self, tmp_path):
        """Repair publishes every valid disk record, not the capped mixed context."""
        cap = 4
        now = time.time()
        path = tmp_path / "C1.jsonl"
        valid_records = [
            {"user": "alice", "text": f"disk{index}", "msg_ts": str(index), "ts": now}
            for index in range(cap)
        ]
        ts_less = {
            "user": "u",
            "text": "invalid",
            "thread_ts": None,
            "msg_ts": None,
            "ts": None,
        }
        path.write_text(
            "".join(json.dumps(record) + "\n" for record in (*valid_records, ts_less)),
            encoding="utf-8",
        )

        history = ChannelHistory(max_entries=2, observe_max_entries=cap, history_dir=tmp_path)
        history.push("C1", "bob", "offmode0")
        history.push("C1", "bob", "offmode1")
        assert all(entry.wall_ts is None for entry in history._channels["C1"])

        history.set_observe("C1")
        _drain_lane()

        assert [entry.text for entry in history._channels["C1"]] == [
            "disk2",
            "disk3",
            "offmode0",
            "offmode1",
        ]
        persisted = [json.loads(line) for line in self._lines(tmp_path)]
        assert [record["text"] for record in persisted] == [
            "disk0",
            "disk1",
            "disk2",
            "disk3",
        ]
        assert all(record["ts"] is not None for record in persisted)

    def test_inherited_file_load_retains_only_twice_cap_before_apply(
        self, tmp_path, monkeypatch, caplog
    ):
        """The lane retains only the newest two windows before the loop fold."""
        cap = 3
        total = 20
        path = tmp_path / "C1.jsonl"
        now = time.time()
        path.write_text(
            "".join(
                json.dumps(
                    {
                        "user": "alice",
                        "text": f"msg{index}",
                        "msg_ts": str(index),
                        "ts": now,
                    }
                )
                + "\n"
                for index in range(total)
            ),
            encoding="utf-8",
        )

        history = ChannelHistory(observe_max_entries=cap, history_dir=tmp_path)
        real_apply = history._apply_observe_load
        retained: list[list[str]] = []
        read_maxlens: list[int | None] = []

        def _recording_apply(channel_id, generation, result, supersedes_terminal):
            assert result is not None
            retained.append([entry.text for entry in result.entries])
            read_maxlens.append(result.entries.maxlen)
            return real_apply(channel_id, generation, result, supersedes_terminal)

        monkeypatch.setattr(history, "_apply_observe_load", _recording_apply)
        with caplog.at_level(logging.INFO, logger=channel_history.__name__):
            history.set_observe("C1")
        _drain_lane()

        assert read_maxlens == [2 * cap]
        assert retained == [[f"msg{index}" for index in range(total - 2 * cap, total)]]
        assert [entry.text for entry in history._channels["C1"]] == [
            f"msg{index}" for index in range(total - cap, total)
        ]
        assert "C1" not in history._load_failed_channels
        assert "C1" not in history._load_pending_channels
        assert f"dropped {total - cap} over cap" in caplog.text
        assert [json.loads(line)["text"] for line in self._lines(tmp_path)] == [
            f"msg{index}" for index in range(total - cap, total)
        ]

    def test_load_compaction_keeps_persisted_entries_outside_the_mixed_window(self, tmp_path):
        """Off-mode context must not evict records from the persisted rewrite."""
        path = tmp_path / "C1.jsonl"
        now = time.time()
        records = [
            {"user": "alice", "text": f"disk{i}", "msg_ts": str(i), "ts": now} for i in range(250)
        ]
        path.write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )

        h = ChannelHistory(max_entries=50, observe_max_entries=200, history_dir=tmp_path)
        for i in range(50):
            h.push("C1", "bob", f"off-mode{i}")

        h.set_observe("C1")
        _drain_lane()

        persisted = [json.loads(line)["text"] for line in self._lines(tmp_path)]
        assert persisted == [f"disk{i}" for i in range(50, 250)]

    def test_under_the_cap_nothing_is_rewritten_or_dropped(self, tmp_path):
        """Negative control: the bound must not fire below the cap."""
        h = ChannelHistory(observe_max_entries=5, history_dir=tmp_path)
        h.set_observe("C1")
        for i in range(4):
            h.push("C1", "alice", f"msg{i}")
        _drain_lane()

        lines = self._lines(tmp_path)
        assert [json.loads(ln)["text"] for ln in lines] == [f"msg{i}" for i in range(4)]

    def test_live_cap_reduction_rewrites_a_quiet_channel_file(self, tmp_path):
        """Lowering the cap republishes the file without waiting for another push."""
        old_cap = 5
        new_cap = 2
        h = ChannelHistory(observe_max_entries=old_cap, history_dir=tmp_path)
        h.set_observe("C1")
        for i in range(old_cap):
            h.push("C1", "alice", f"msg{i}")
        _drain_lane()
        assert len(self._lines(tmp_path)) == old_cap

        h.set_observe_limits(new_cap, channel_history.OBSERVE_TTL_SECS)
        _drain_lane()

        lines = self._lines(tmp_path)
        assert len(lines) <= new_cap
        assert [json.loads(line)["text"] for line in lines] == ["msg3", "msg4"]

    @pytest.mark.parametrize("window_state", ["empty", "absent"])
    def test_live_cap_reduction_never_unlinks_a_file_the_window_did_not_load(
        self, tmp_path, window_state
    ):
        """An empty window beside a full file (a failed load) is left alone.

        A REWRITE snapshotted from an empty buffer is content=None, which the
        worker executes as an unlink — so the cap-reduction rewrite must be
        gated on the window, or lowering the cap after a boot-time read
        failure would delete the channel's entire history.
        """
        h = ChannelHistory(observe_max_entries=5, history_dir=tmp_path)
        h.set_observe("C1")
        for i in range(5):
            h.push("C1", "alice", f"msg{i}")
        _drain_lane()
        path = tmp_path / "C1.jsonl"
        original = path.read_text(encoding="utf-8")
        assert len(original.splitlines()) == 5

        # Simulate a load that failed to read the file: observe is on, the
        # file is intact, and nothing reached the in-memory window.
        if window_state == "empty":
            h._channels["C1"].clear()
        else:
            h._channels.pop("C1")
        assert "C1" in h._observe_channels

        h.set_observe_limits(2, channel_history.OBSERVE_TTL_SECS)
        _drain_lane()

        assert path.exists(), "a cap reduction unlinked a file the window never loaded"
        assert path.read_text(encoding="utf-8") == original

    def test_live_cap_reduction_with_a_ts_less_window_keeps_the_file(self, tmp_path):
        """A window of non-persistable entries must not unlink the residue file.

        Messages received while observe was off carry ``wall_ts=None``; the
        worker skips them when it serializes a REWRITE, so a snapshot made of
        nothing else is content=None — an UNLINK. After a load merges disk
        entries ahead of those in-memory ones, the newest slice of the capped
        window can be entirely ts-less, and lowering the cap from that window
        must leave the file exactly as it was, not remove it.
        """
        old_cap = 4
        seed = ChannelHistory(observe_max_entries=old_cap, history_dir=tmp_path)
        seed.set_observe("C1")
        for index in range(old_cap):
            seed.push("C1", "alice", f"persisted{index}")
        _drain_lane()
        path = tmp_path / "C1.jsonl"
        original = path.read_text(encoding="utf-8")
        assert len(original.splitlines()) == old_cap

        # A fresh process hears the channel BEFORE observe is enabled...
        history = ChannelHistory(observe_max_entries=old_cap, history_dir=tmp_path)
        for index in range(old_cap):
            history.push("C1", "bob", f"offmode{index}")
        assert all(entry.wall_ts is None for entry in history._channels["C1"])
        # ...then loads the file: disk entries land first, the ts-less ones
        # after, so the capped window now holds only non-persistable entries.
        history.set_observe("C1")
        _drain_lane()
        assert "C1" not in history._load_failed_channels
        assert all(entry.wall_ts is None for entry in history._channels["C1"])
        assert path.read_text(encoding="utf-8") == original

        history.set_observe_limits(2, channel_history.OBSERVE_TTL_SECS)
        _drain_lane()

        assert path.exists(), "a cap reduction unlinked the file behind a ts-less window"
        assert path.read_text(encoding="utf-8") == original

    def test_live_cap_reduction_with_a_mixed_window_keeps_displaced_disk_records(self, tmp_path):
        """A cap reduction must not publish a window that still holds ts-less entries.

        With ``[o3, off0, off1, new0]`` in memory — one surviving disk record,
        two messages heard before observe was enabled, one new message — the
        persistable slice ``[o3, new0]`` looks like a valid cap-2 window, but
        the ts-less entries displaced ``o0..o2`` out of the deque, so publishing
        it would replace the file with a strict subset of its own content. The
        shrink must wait for the next complete-view rewrite: the count trigger,
        once a cap of new messages has evicted the ts-less entries.
        """
        cap = 4
        seed = ChannelHistory(observe_max_entries=cap, history_dir=tmp_path)
        seed.set_observe("C1")
        for index in range(cap):
            seed.push("C1", "alice", f"o{index}", msg_ts=f"o{index}")
        _drain_lane()
        path = tmp_path / "C1.jsonl"
        original = path.read_text(encoding="utf-8")
        assert [json.loads(line)["text"] for line in original.splitlines()] == [
            "o0",
            "o1",
            "o2",
            "o3",
        ]

        h = ChannelHistory(max_entries=2, observe_max_entries=cap, history_dir=tmp_path)
        h.push("C1", "bob", "off0")
        h.push("C1", "bob", "off1")
        h.set_observe("C1")
        _drain_lane()
        h.push("C1", "alice", "new0", msg_ts="new0")
        _drain_lane()
        assert [entry.text for entry in h._channels["C1"]] == ["o3", "off0", "off1", "new0"]
        assert [json.loads(line)["text"] for line in self._lines(tmp_path)] == [
            "o0",
            "o1",
            "o2",
            "o3",
            "new0",
        ]

        h.set_observe_limits(2, channel_history.OBSERVE_TTL_SECS)
        _drain_lane()

        persisted = [json.loads(line)["text"] for line in self._lines(tmp_path)]
        assert "o3" in persisted, "the cap reduction destroyed a disk record the window displaced"
        assert persisted == [
            "o0",
            "o1",
            "o2",
            "o3",
            "new0",
        ], "a window still holding ts-less entries was published"
        assert h._channels["C1"].maxlen == 2

        # The next complete-view rewrite shrinks the file: one more append fills
        # the cap-2 count window, and by then the deque is all-new.
        h.push("C1", "alice", "new1", msg_ts="new1")
        _drain_lane()
        assert [entry.text for entry in h._channels["C1"]] == ["new0", "new1"]
        assert [json.loads(line)["text"] for line in self._lines(tmp_path)] == ["new0", "new1"]

    def test_saturation_compaction_keeps_disk_records_displaced_by_a_ts_less_window(
        self, tmp_path, monkeypatch
    ):
        """A rewrite snapshotted from a window still holding ts-less entries must not shrink the file.

        Messages heard before observe was enabled carry ``wall_ts=None``. The
        load merge appends them AFTER the disk entries, so in a full deque they
        displace the oldest persisted records, and every push that follows
        evicts another disk record while they stay. A saturation-triggered
        compaction takes its snapshot from that deque with no explicit list:
        the worker drops the ts-less entries, and the file would be replaced by
        a strict subset of its own window. The displaced records must survive,
        and the count trigger must still bound the file once the ts-less
        entries have been pushed out.
        """
        from kiro_crew.executors import channel_history_executor

        cap = 4
        seed = ChannelHistory(observe_max_entries=cap, history_dir=tmp_path)
        seed.set_observe("C1")
        for index in range(cap):
            seed.push("C1", "alice", f"original{index}", msg_ts=f"o{index}")
        _drain_lane()
        path = tmp_path / "C1.jsonl"
        original = path.read_text(encoding="utf-8")
        assert len(original.splitlines()) == cap

        # A fresh process hears two messages before observe is enabled...
        h = ChannelHistory(max_entries=2, observe_max_entries=cap, history_dir=tmp_path)
        h.push("C1", "bob", "offmode0")
        h.push("C1", "bob", "offmode1")
        # ...then the load lands the disk entries first and the ts-less ones
        # after them, displacing the two oldest persisted records.
        h.set_observe("C1")
        _drain_lane()
        assert [entry.text for entry in h._channels["C1"]] == [
            "original2",
            "original3",
            "offmode0",
            "offmode1",
        ]
        assert path.read_text(encoding="utf-8") == original, "the load itself rewrote the file"

        monkeypatch.setattr(channel_history, "_DISK_LANE_MAX_PENDING", 1)
        h._disk_lane_slots = threading.Semaphore(1)
        gate = threading.Event()
        channel_history_executor().submit(gate.wait, 30)  # wedge the worker
        try:
            h.push("C1", "alice", "new0", msg_ts="n0")  # takes the single slot
            h.push("C1", "alice", "new1", msg_ts="n1")  # saturates: coalesced rewrite
        finally:
            gate.set()
        _drain_lane()

        persisted = [json.loads(line)["text"] for line in self._lines(tmp_path)]
        assert {"original2", "original3"} <= set(
            persisted
        ), "the saturation rewrite destroyed disk records displaced by the ts-less window"
        assert "new0" in persisted, "the admitted append never landed"

        # Once a cap of new messages has evicted the ts-less entries, the count
        # trigger publishes the complete window — including the refused append.
        h.push("C1", "alice", "new2", msg_ts="n2")
        h.push("C1", "alice", "new3", msg_ts="n3")
        _drain_lane()
        assert [json.loads(line)["text"] for line in self._lines(tmp_path)] == [
            "new0",
            "new1",
            "new2",
            "new3",
        ]

    def test_failed_load_suppresses_subset_rewrites_until_successful_reload(
        self, tmp_path, monkeypatch
    ):
        """A transient read failure protects the unseen persisted prefix."""
        original_cap = 4
        seed = ChannelHistory(observe_max_entries=original_cap, history_dir=tmp_path)
        seed.set_observe("C1")
        for index in range(original_cap):
            seed.push("C1", "alice", f"original{index}")
        _drain_lane()

        path = tmp_path / "C1.jsonl"
        original_texts = {f"original{index}" for index in range(original_cap)}
        assert {json.loads(line)["text"] for line in self._lines(tmp_path)} == original_texts

        history = ChannelHistory(observe_max_entries=original_cap, history_dir=tmp_path)
        real_open = history._open_observe_file
        fail_load = True

        def _controlled_open(path, root_identity):
            if fail_load:
                raise OSError("transient read failure injected by test")
            return real_open(path, root_identity)

        monkeypatch.setattr(history, "_open_observe_file", _controlled_open)
        history.set_observe("C1")
        assert "C1" in history._load_failed_channels

        history._compact("C1")
        _drain_lane()
        persisted = {json.loads(line)["text"] for line in self._lines(tmp_path)}
        assert original_texts <= persisted, "central compaction erased unseen history"

        history.push("C1", "bob", "new0")
        _drain_lane()
        history.set_observe_limits(2, channel_history.OBSERVE_TTL_SECS)
        _drain_lane()
        persisted = {json.loads(line)["text"] for line in self._lines(tmp_path)}
        assert original_texts <= persisted, "cap reduction erased history unseen by the window"

        history.push("C1", "bob", "new1")
        _drain_lane()
        persisted = {json.loads(line)["text"] for line in self._lines(tmp_path)}
        assert original_texts <= persisted, "count compaction erased history unseen by the window"
        # The count trigger spent its window on a load retry (which failed
        # again, so suppression held); the next retry is a cap of appends away.
        assert history._appends_since_compact["C1"] == 0
        assert "C1" in history._load_failed_channels

        fail_load = False
        history.set_observe("C1")
        _drain_lane()

        assert "C1" not in history._load_failed_channels
        assert [json.loads(line)["text"] for line in self._lines(tmp_path)] == ["new0", "new1"]
        assert path.exists()

    def test_failed_load_suppresses_appends_without_growing_the_file(self, tmp_path, monkeypatch):
        """A failed load freezes every disk write while memory keeps moving."""
        path = tmp_path / "C1.jsonl"
        original = (
            json.dumps({"user": "alice", "text": "persisted", "msg_ts": "1", "ts": time.time()})
            + "\n"
        )
        path.write_text(original, encoding="utf-8")

        history = ChannelHistory(observe_max_entries=10, history_dir=tmp_path)
        monkeypatch.setattr(
            history,
            "_open_observe_file",
            lambda path, root_identity: (_ for _ in ()).throw(OSError("injected read failure")),
        )
        history.set_observe("C1")
        assert "C1" in history._load_failed_channels

        for index in range(3):
            history.push("C1", "bob", f"memory{index}")
        _drain_lane()

        assert path.read_text(encoding="utf-8") == original
        assert [entry.text for entry in history._channels["C1"]] == [
            "memory0",
            "memory1",
            "memory2",
        ]

    def test_torn_ascii_json_tail_is_rewritten_before_next_append(self, tmp_path):
        """A torn ASCII JSON tail is purged before a later append can join it."""
        path = tmp_path / "C1.jsonl"
        valid = {
            "user": "alice",
            "text": "persisted",
            "msg_ts": "1",
            "ts": time.time(),
        }
        torn_tail = b'{"user":"torn","text":"partial"'
        path.write_bytes(json.dumps(valid).encode("utf-8") + b"\n" + torn_tail)

        history = ChannelHistory(observe_max_entries=2, history_dir=tmp_path)
        history.set_observe("C1")

        assert "C1" not in history._load_failed_channels
        assert [entry.text for entry in history._channels["C1"]] == ["persisted"]
        _drain_lane()
        assert torn_tail not in path.read_bytes()

        history.push("C1", "bob", "appended", msg_ts="2")
        _drain_lane()

        reloaded = ChannelHistory(observe_max_entries=2, history_dir=tmp_path)
        reloaded.set_observe("C1")
        assert "C1" not in reloaded._load_failed_channels
        assert [entry.text for entry in reloaded._channels["C1"]] == [
            "persisted",
            "appended",
        ]

    def test_torn_utf8_tail_loads_valid_prefix_and_rewrites_file(self, tmp_path):
        """A torn multibyte tail is skipped and purged without suppressing persistence."""
        path = tmp_path / "C1.jsonl"
        valid = {
            "user": "alice",
            "text": "persisted",
            "msg_ts": "1",
            "ts": time.time(),
        }
        torn_tail = b'{"user":"u","text":"\xf0\x9f'
        path.write_bytes(json.dumps(valid).encode("utf-8") + b"\n" + torn_tail)

        history = ChannelHistory(observe_max_entries=2, history_dir=tmp_path)
        history.set_observe("C1")

        assert "C1" not in history._load_failed_channels
        assert "C1" not in history._load_pending_channels
        assert [entry.text for entry in history._channels["C1"]] == ["persisted"]

        history.push("C1", "bob", "memory0")
        _drain_lane()

        persisted_bytes = path.read_bytes()
        assert torn_tail not in persisted_bytes
        persisted = [json.loads(line.decode("utf-8")) for line in persisted_bytes.splitlines()]
        assert [entry["text"] for entry in persisted] == ["persisted", "memory0"]

    def test_undecodable_mid_file_line_is_skipped_and_rewritten(self, tmp_path):
        """An undecodable line is malformed content, not an incomplete file view."""
        path = tmp_path / "C1.jsonl"
        now = time.time()
        before = {"user": "alice", "text": "before", "msg_ts": "1", "ts": now}
        after = {"user": "bob", "text": "after", "msg_ts": "2", "ts": now}
        undecodable = b'{"user":"bad","text":"\xff"}\n'
        path.write_bytes(
            json.dumps(before).encode("utf-8")
            + b"\n"
            + undecodable
            + json.dumps(after).encode("utf-8")
            + b"\n"
        )

        history = ChannelHistory(history_dir=tmp_path)
        history.set_observe("C1")

        assert "C1" not in history._load_failed_channels
        assert [entry.text for entry in history._channels["C1"]] == ["before", "after"]
        _drain_lane()

        persisted = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        assert [entry["text"] for entry in persisted] == ["before", "after"]

    def test_failed_load_recovers_on_a_later_load_of_the_same_observe_session(
        self, tmp_path, monkeypatch
    ):
        """A transient read failure must not disable persistence for the process.

        The gateway never re-issues ``set_observe`` for a channel that is
        already observing, so the retry that lifts suppression is a later load
        of the SAME observe session (same generation). Once that load folds
        the complete file into memory, appends and count compaction resume.
        """
        cap = 3
        path = tmp_path / "C1.jsonl"
        original = (
            json.dumps({"user": "alice", "text": "persisted", "msg_ts": "1", "ts": time.time()})
            + "\n"
        )
        path.write_text(original, encoding="utf-8")

        history = ChannelHistory(observe_max_entries=cap, history_dir=tmp_path)
        real_open = history._open_observe_file
        fail_load = True

        def _controlled_open(path, root_identity):
            if fail_load:
                raise OSError("transient read failure injected by test")
            return real_open(path, root_identity)

        monkeypatch.setattr(history, "_open_observe_file", _controlled_open)
        history.set_observe("C1")
        assert "C1" in history._load_failed_channels
        generation = history._observe_generation["C1"]

        history.push("C1", "bob", "memory0")
        _drain_lane()
        assert path.read_text(encoding="utf-8") == original, "a failed load did not suppress"

        fail_load = False
        history._load_observe("C1")
        _drain_lane()

        assert history._observe_generation["C1"] == generation, "the retry was a fresh session"
        assert "C1" not in history._load_failed_channels
        assert "C1" not in history._load_pending_channels
        assert [entry.text for entry in history._channels["C1"]] == ["persisted", "memory0"]

        history.push("C1", "bob", "memory1")
        _drain_lane()
        assert [json.loads(line)["text"] for line in self._lines(tmp_path)] == [
            "persisted",
            "memory1",
        ], "appends did not resume after the successful reload"

        history.push("C1", "bob", "memory2")
        history.push("C1", "bob", "memory3")
        _drain_lane()
        assert history._appends_since_compact["C1"] == 0
        assert [json.loads(line)["text"] for line in self._lines(tmp_path)] == [
            "memory1",
            "memory2",
            "memory3",
        ], "count compaction did not resume after the successful reload"

    def test_failed_load_is_retried_on_the_next_count_trigger(self, tmp_path, monkeypatch):
        """One transient read failure must not suppress the channel for the process.

        Nothing but ``set_observe`` issues a load, and the gateway issues it once
        per channel, so the retry has to be armed off the traffic the channel
        already generates: the count trigger. Below the threshold nothing
        re-reads the file; at the threshold the deferred load runs once more on
        the lane, and a success lifts the suppression, republishes the window
        the suppressed appends never reached, and lets appends resume.
        """
        cap = 3
        path = tmp_path / "C1.jsonl"
        original = (
            json.dumps({"user": "alice", "text": "persisted", "msg_ts": "1", "ts": time.time()})
            + "\n"
        )
        path.write_text(original, encoding="utf-8")

        history = ChannelHistory(observe_max_entries=cap, history_dir=tmp_path)
        real_open = history._open_observe_file
        failures = [OSError(errno.EMFILE, "too many open files")]
        opens = 0

        def _transient_open(path, root_identity):
            nonlocal opens
            opens += 1
            if failures:
                raise failures.pop()
            return real_open(path, root_identity)

        monkeypatch.setattr(history, "_open_observe_file", _transient_open)
        history.set_observe("C1")
        assert "C1" in history._load_failed_channels
        assert opens == 1

        history.push("C1", "bob", "memory0")
        history.push("C1", "bob", "memory1")
        _drain_lane()
        assert path.read_text(encoding="utf-8") == original, "a failed load did not suppress"
        assert opens == 1, "the load was retried below the count threshold"

        history.push("C1", "bob", "memory2")  # count trigger: the one retry this window
        _drain_lane()

        assert opens == 2, "the count trigger did not retry the failed load"
        assert "C1" not in history._load_failed_channels, "a successful retry kept suppression"
        assert "C1" not in history._load_pending_channels
        assert history._appends_since_compact["C1"] == 0
        assert [json.loads(line)["text"] for line in self._lines(tmp_path)] == [
            "memory0",
            "memory1",
            "memory2",
        ], "the window the suppressed appends never reached was not republished"

        history.push("C1", "bob", "memory3")
        _drain_lane()
        assert opens == 2, "a successful retry was retried again"
        assert [json.loads(line)["text"] for line in self._lines(tmp_path)] == [
            "memory0",
            "memory1",
            "memory2",
            "memory3",
        ], "appends did not resume after the retried load"

    def test_failed_load_retry_is_bounded_to_one_per_count_window(self, tmp_path, monkeypatch):
        """A load that keeps failing is re-read once per cap of appends, not per push."""
        cap = 2
        path = tmp_path / "C1.jsonl"
        original = (
            json.dumps({"user": "alice", "text": "persisted", "msg_ts": "1", "ts": time.time()})
            + "\n"
        )
        path.write_text(original, encoding="utf-8")

        history = ChannelHistory(observe_max_entries=cap, history_dir=tmp_path)
        opens = 0

        def _failing_open(path, root_identity):
            nonlocal opens
            opens += 1
            raise OSError(errno.EIO, "injected read failure")

        monkeypatch.setattr(history, "_open_observe_file", _failing_open)
        history.set_observe("C1")
        assert opens == 1

        for index in range(2 * cap):
            history.push("C1", "bob", f"memory{index}")
        _drain_lane()

        assert opens == 3, "the retry was not bounded to one per count window"
        assert "C1" in history._load_failed_channels, "a failed retry lifted suppression"
        assert "C1" not in history._load_pending_channels
        assert path.read_text(encoding="utf-8") == original

    def test_absent_file_load_lifts_failed_load_suppression(self, tmp_path, monkeypatch):
        """A load that finds no file is a complete read of nothing, not a failure.

        With no file there is no unseen persisted prefix to protect, so a
        retry that lands on ``FileNotFoundError`` must lift the suppression a
        transient failure armed — otherwise the channel never persists again.
        """
        path = tmp_path / "C1.jsonl"
        history = ChannelHistory(observe_max_entries=4, history_dir=tmp_path)
        real_open = history._open_observe_file
        fail_load = True

        def _controlled_open(path, root_identity):
            if fail_load:
                raise OSError("transient read failure injected by test")
            return real_open(path, root_identity)

        monkeypatch.setattr(history, "_open_observe_file", _controlled_open)
        history.set_observe("C1")
        assert "C1" in history._load_failed_channels

        history.push("C1", "bob", "memory0")
        _drain_lane()
        assert not path.exists(), "a failed load did not suppress the append"

        fail_load = False
        history.set_observe("C1")
        _drain_lane()

        assert "C1" not in history._load_failed_channels
        assert "C1" not in history._load_pending_channels
        assert not path.exists()

        history.push("C1", "bob", "memory1")
        _drain_lane()
        assert path.exists(), "an absent-file load left the channel suppressed"
        assert [json.loads(line)["text"] for line in self._lines(tmp_path)] == ["memory1"]

    def test_oserror_load_retry_stays_suppressed(self, tmp_path, monkeypatch):
        """A genuine read failure keeps failed-load suppression armed."""
        path = tmp_path / "C1.jsonl"
        original = (
            json.dumps({"user": "alice", "text": "persisted", "msg_ts": "1", "ts": time.time()})
            + "\n"
        ).encode("utf-8")
        path.write_bytes(original)

        history = ChannelHistory(observe_max_entries=4, history_dir=tmp_path)
        monkeypatch.setattr(
            history,
            "_open_observe_file",
            lambda path, root_identity: (_ for _ in ()).throw(OSError("injected read failure")),
        )
        history.set_observe("C1")
        assert "C1" in history._load_failed_channels

        history._load_observe("C1")
        _drain_lane()
        assert "C1" in history._load_failed_channels, "a failed retry lifted suppression"
        assert "C1" not in history._load_pending_channels

        history.push("C1", "bob", "memory0")
        history._compact("C1")
        _drain_lane()
        assert path.read_bytes() == original
        assert [entry.text for entry in history._channels["C1"]] == ["memory0"]

    def test_live_cap_increase_resizes_the_existing_observe_buffer(self, tmp_path):
        """Raising the cap grows an existing channel instead of keeping its stale maxlen."""
        h = ChannelHistory(observe_max_entries=2, history_dir=tmp_path)
        h.set_observe("C1")
        h.push("C1", "alice", "msg0")
        h.push("C1", "alice", "msg1")
        _drain_lane()

        h.set_observe_limits(4, channel_history.OBSERVE_TTL_SECS)
        h.push("C1", "alice", "msg2")
        h.push("C1", "alice", "msg3")
        _drain_lane()

        assert h._channels["C1"].maxlen == 4
        assert [entry.text for entry in h._channels["C1"]] == [
            "msg0",
            "msg1",
            "msg2",
            "msg3",
        ]
        assert [json.loads(ln)["text"] for ln in self._lines(tmp_path)] == [
            "msg0",
            "msg1",
            "msg2",
            "msg3",
        ]


class TestObserveCompactionOffLoop:
    """Compaction's ``atomic_write`` must never run on the event-loop thread.

    ``push`` and ``set_observe`` are called synchronously from the gateway's
    async handlers, so every compaction trigger fires while a loop is running.
    The caller snapshots the entry list (the only mutator of the deque),
    while the worker performs both serialization and the file write.
    """

    @staticmethod
    def _read_lines(path) -> list[str]:
        """Read the file, riding out the Windows sharing window.

        The compaction lands via ``os.replace``; on Windows an AV or indexer
        handle on the freshly renamed file makes an immediate open fail with
        ``PermissionError`` while nothing is wrong — the same transient
        ``atomic_write.replace_with_retry`` rides out on the write side.
        Bounded retry, then the real error surfaces.
        """
        for _ in range(20):
            try:
                return path.read_text(encoding="utf-8").strip().splitlines()
            except PermissionError:
                time.sleep(0.1)
        return path.read_text(encoding="utf-8").strip().splitlines()

    @staticmethod
    def _record_writes(monkeypatch):
        """Patch atomic_write to record the writing thread and signal completion."""
        from kiro_crew import channel_history as ch_mod

        seen: dict = {}
        wrote = threading.Event()
        real = ch_mod.atomic_write

        def _recording(path, content, *args, **kwargs):
            seen["ident"] = threading.get_ident()
            try:
                return real(path, content, *args, **kwargs)
            finally:
                wrote.set()

        monkeypatch.setattr(ch_mod, "atomic_write", _recording)
        return seen, wrote

    @pytest.mark.asyncio
    async def test_history_root_resolution_is_deferred_to_the_lane_once(
        self, tmp_path, monkeypatch
    ):
        """Construction does no IO; first lane use resolves and trusts once."""
        root_type = type(tmp_path)
        real_resolve = root_type.resolve
        calls: list[tuple[object, int, str]] = []

        def _recording_resolve(path, *args, **kwargs):
            if path == tmp_path:
                calls.append((path, threading.get_ident(), threading.current_thread().name))
            return real_resolve(path, *args, **kwargs)

        monkeypatch.setattr(root_type, "resolve", _recording_resolve)
        history = ChannelHistory(history_dir=tmp_path)
        assert calls == [], "ChannelHistory.__init__ resolved the root"

        history.set_observe("C1")
        await self._drain_disk_lane()
        history.push("C1", "alice", "one")
        history.push("C1", "alice", "two")
        await self._drain_disk_lane()

        assert len(calls) == 1
        assert calls[0][1] != threading.get_ident()
        assert calls[0][2].startswith("mc-chan-hist")

    @pytest.mark.asyncio
    async def test_a_load_result_landing_after_observe_off_cannot_resurrect_the_file(
        self, tmp_path
    ):
        """A stale deferred load must not repopulate or republish a disabled channel.

        The oversized file makes a successful load schedule a compaction
        REWRITE. If observe is switched off before the deferred result is
        applied, that rewrite would coalesce over the user's UNLINK and bring
        the removed file back; the load generation check drops the stale result.
        """
        path = tmp_path / "C1.jsonl"
        now = time.time()
        path.write_text(
            "".join(
                json.dumps({"user": "alice", "text": f"msg{i}", "msg_ts": str(i), "ts": now}) + "\n"
                for i in range(10)
            ),
            encoding="utf-8",
        )
        h = ChannelHistory(observe_max_entries=5, history_dir=tmp_path)
        h.set_observe("C1")  # result still pending on the lane
        h.unset_observe("C1")  # same tick: queues the UNLINK behind the read
        await self._drain_disk_lane()

        assert not path.exists(), "a stale load result resurrected the unlinked file"
        assert h.entry_count("C1") == 0, "a stale load result repopulated a disabled channel"

    @pytest.mark.asyncio
    async def test_pending_load_replays_only_suppressed_compaction(self, tmp_path):
        """A burst during deferred load cannot publish its post-boot subset."""
        from kiro_crew.executors import channel_history_executor

        path = tmp_path / "C1.jsonl"
        now = time.time()
        original = "".join(
            json.dumps({"user": "alice", "text": f"disk{i}", "msg_ts": str(i), "ts": now}) + "\n"
            for i in range(2)
        )
        path.write_text(original, encoding="utf-8")

        h = ChannelHistory(observe_max_entries=2, history_dir=tmp_path)
        gate = threading.Event()
        channel_history_executor().submit(gate.wait, 30)
        try:
            h.set_observe("C1")
            assert "C1" in h._load_pending_channels

            h.push("C1", "bob", "new0", msg_ts="new0")
            h.push("C1", "bob", "new1", msg_ts="new1")

            assert "C1" in h._load_pending_compactions
            assert path.read_text(encoding="utf-8") == original
        finally:
            gate.set()

        await self._drain_disk_lane()

        assert "C1" not in h._load_pending_channels
        assert "C1" not in h._load_pending_compactions
        assert [json.loads(line)["text"] for line in self._read_lines(path)] == [
            "new0",
            "new1",
        ]

    @pytest.mark.asyncio
    async def test_deferred_load_applies_current_limits_on_loop_thread(self, tmp_path, monkeypatch):
        """A queued load uses the limits in force when its result is applied."""
        wall_time = time.time()
        raised_path = tmp_path / "RAISE.jsonl"
        raised_path.write_text(
            "".join(
                json.dumps(
                    {
                        "user": "alice",
                        "text": f"raised{index}",
                        "msg_ts": str(index),
                        "ts": wall_time - age,
                    }
                )
                + "\n"
                for index, age in enumerate((20, 15, 10, 5))
            ),
            encoding="utf-8",
        )
        raised = ChannelHistory(
            observe_max_entries=2,
            observe_ttl_secs=1,
            history_dir=tmp_path,
        )
        raised_started = threading.Event()
        raised_release = threading.Event()
        raised_open = raised._open_observe_file

        def _hold_raised_open(path, root_identity):
            opened = raised_open(path, root_identity)
            raised_started.set()
            if not raised_release.wait(10):
                opened.close()
                raise TimeoutError("test did not release the raised-limit load")
            return opened

        monkeypatch.setattr(raised, "_open_observe_file", _hold_raised_open)
        raised.set_observe("RAISE")
        assert await asyncio.to_thread(raised_started.wait, 10)
        raised.set_observe_limits(4, 60)
        raised_release.set()
        await self._drain_disk_lane()

        assert [entry.text for entry in raised._channels["RAISE"]] == [
            "raised0",
            "raised1",
            "raised2",
            "raised3",
        ]

        lowered_path = tmp_path / "LOWER.jsonl"
        lowered_path.write_text(
            "".join(
                json.dumps(
                    {
                        "user": "bob",
                        "text": f"lowered{index}",
                        "msg_ts": str(index),
                        "ts": wall_time,
                    }
                )
                + "\n"
                for index in range(5)
            ),
            encoding="utf-8",
        )
        lowered = ChannelHistory(
            observe_max_entries=5,
            observe_ttl_secs=60,
            history_dir=tmp_path,
        )
        lowered_started = threading.Event()
        lowered_release = threading.Event()
        lowered_open = lowered._open_observe_file

        def _hold_lowered_open(path, root_identity):
            opened = lowered_open(path, root_identity)
            lowered_started.set()
            if not lowered_release.wait(10):
                opened.close()
                raise TimeoutError("test did not release the lowered-limit load")
            return opened

        monkeypatch.setattr(lowered, "_open_observe_file", _hold_lowered_open)
        lowered.set_observe("LOWER")
        assert await asyncio.to_thread(lowered_started.wait, 10)
        lowered.push("LOWER", "bob", "expired-live", msg_ts="live")
        lowered._channels["LOWER"][-1].wall_ts = wall_time - 30
        lowered.set_observe_limits(2, 10)
        lowered_release.set()
        await self._drain_disk_lane()

        assert [entry.text for entry in lowered._channels["LOWER"]] == [
            "lowered3",
            "lowered4",
        ]
        assert [json.loads(line)["text"] for line in self._read_lines(lowered_path)] == [
            "lowered3",
            "lowered4",
        ]

    @pytest.mark.asyncio
    async def test_absent_load_replay_is_persistable_deduped_and_capped(self, tmp_path):
        """A suppressed replay publishes one current-cap persistable window."""
        from kiro_crew.channel_history import HistoryEntry
        from kiro_crew.executors import channel_history_executor

        cap = 3
        history = ChannelHistory(observe_max_entries=cap, history_dir=tmp_path)
        release = threading.Event()
        channel_history_executor().submit(release.wait, 30)
        try:
            history.set_observe("C1")
            for index in range(cap):
                history.push("C1", "alice", f"msg{index}", msg_ts=str(index))
            assert "C1" in history._load_pending_compactions

            history._load_pending_compaction_snapshots["C1"].append(
                HistoryEntry(user="offmode", text="not persistable", wall_ts=None)
            )
            history.push("C1", "alice", "msg3", msg_ts="3")
            history.push("C1", "alice", "msg4", msg_ts="4")
            assert history._appends_since_compact["C1"] == 2
        finally:
            release.set()
        await self._drain_disk_lane()

        persisted = [json.loads(line) for line in self._read_lines(tmp_path / "C1.jsonl")]
        assert [entry["text"] for entry in persisted] == ["msg2", "msg3", "msg4"]
        assert len({entry["msg_ts"] for entry in persisted}) == cap
        assert all(entry["ts"] is not None for entry in persisted)
        assert history._appends_since_compact["C1"] == 0

    @pytest.mark.asyncio
    async def test_push_schedules_no_history_root_io_on_event_loop(self, tmp_path, monkeypatch):
        """Append and compaction scheduling only copy the control-plane identity."""
        h = ChannelHistory(observe_max_entries=1, history_dir=tmp_path)
        h.set_observe("C1")
        loop_thread = threading.get_ident()
        calls: list[tuple[str, int]] = []
        real_pin_directory = platform_compat.pin_directory
        real_fstat = os.fstat

        def _pin_directory(path):
            thread = threading.get_ident()
            calls.append(("pin_directory", thread))
            assert thread != loop_thread, "push pinned the history root on the event loop"
            return real_pin_directory(path)

        def _fstat(fd):
            thread = threading.get_ident()
            calls.append(("fstat", thread))
            assert thread != loop_thread, "push fstat'd the history root on the event loop"
            return real_fstat(fd)

        with monkeypatch.context() as filesystem_probe:
            filesystem_probe.setattr(platform_compat, "pin_directory", _pin_directory)
            filesystem_probe.setattr(os, "fstat", _fstat)

            h.push("C1", "alice", "triggers append and compaction")
            await self._drain_disk_lane()

        assert {name for name, _thread in calls} == {"pin_directory", "fstat"}

    def test_history_root_snapshot_never_blocks_on_preparer(self, tmp_path):
        """A loop-thread snapshot never waits on a preparer's filesystem IO.

        ``_prepare_history_root`` holds ``_history_root_lock`` across
        resolve/mkdir/pin/fstat on the lane. The loop-thread callers copy the
        root through ``_history_root_snapshot``; while the preparer holds the
        lock they must get the pre-trust answer (``None``) at once, not wait
        on lane IO — the lane prepares the root itself for a ``None`` carrier.
        """
        h = ChannelHistory(observe_max_entries=100, history_dir=tmp_path)
        h.set_observe("C1")
        _drain_lane()  # first trust already captured on the lane
        held = threading.Event()
        release = threading.Event()

        def _preparer() -> None:
            with h._history_root_lock:  # what _prepare_history_root holds across its IO
                held.set()
                release.wait(3)  # bounded: a regressed snapshot fails fast instead of hanging

        preparer = threading.Thread(target=_preparer, daemon=True)
        preparer.start()
        assert held.wait(5), "the preparer thread never took the root lock"
        try:
            started = time.monotonic()
            snapshot = h._history_root_snapshot()
            elapsed = time.monotonic() - started
        finally:
            release.set()
            preparer.join(timeout=5)
        assert snapshot is None, "the snapshot must answer pre-trust while the lock is held"
        assert elapsed < 1.0, f"the root snapshot waited {elapsed:.1f}s on the preparer"

        # A None snapshot only moves the root copy onto the lane: nothing is dropped.
        h.push("C1", "alice", "lands once the preparer is done")
        _drain_lane()
        assert "lands once the preparer is done" in (tmp_path / "C1.jsonl").read_text(
            encoding="utf-8"
        )

    @pytest.mark.asyncio
    async def test_terminal_serialization_runs_off_the_loop_thread(self, tmp_path, monkeypatch):
        """A full-window rewrite does not serialize on the event loop."""
        from kiro_crew import channel_history as ch_mod
        from kiro_crew.executors import channel_history_executor

        h = ChannelHistory(observe_max_entries=5, history_dir=tmp_path)
        h.set_observe("C1")
        h.push("C1", "alice", "msg0")
        await self._drain_disk_lane()

        seen: list[int] = []
        real_dumps = ch_mod.json.dumps

        def _recording_dumps(*args, **kwargs):
            seen.append(threading.get_ident())
            return real_dumps(*args, **kwargs)

        monkeypatch.setattr(ch_mod.json, "dumps", _recording_dumps)
        gate = threading.Event()
        channel_history_executor().submit(gate.wait, 30)
        try:
            h._compact("C1")
            assert not seen, "terminal serialization ran while scheduling"
        finally:
            gate.set()
        await self._drain_disk_lane()

        assert seen
        assert all(ident != threading.get_ident() for ident in seen)

    @pytest.mark.asyncio
    async def test_append_path_compaction_writes_off_the_loop_thread(self, tmp_path, monkeypatch):
        """The count-triggered compaction in ``_append_to_disk`` is offloaded."""
        seen, wrote = self._record_writes(monkeypatch)

        h = ChannelHistory(observe_max_entries=5, history_dir=tmp_path)
        h.set_observe("C1")
        for i in range(5):
            h.push("C1", "alice", f"msg{i}")

        assert await asyncio.to_thread(wrote.wait, 10), "compaction write never ran"
        assert (
            seen["ident"] != threading.get_ident()
        ), "compaction ran its atomic_write on the event-loop thread"
        lines = self._read_lines(tmp_path / "C1.jsonl")
        assert [json.loads(ln)["text"] for ln in lines] == [f"msg{i}" for i in range(5)]

    @pytest.mark.asyncio
    async def test_load_path_compaction_writes_off_the_loop_thread(self, tmp_path, monkeypatch):
        """The size-triggered compaction in ``_load_observe`` is offloaded too."""
        path = tmp_path / "C1.jsonl"
        now = time.time()
        with path.open("w", encoding="utf-8") as f:
            for i in range(40):
                f.write(
                    json.dumps(
                        {"user": "alice", "text": f"msg{i}", "thread_ts": None, "ts": now - 1}
                    )
                    + "\n"
                )

        seen, wrote = self._record_writes(monkeypatch)

        h = ChannelHistory(observe_max_entries=5, history_dir=tmp_path)
        h.set_observe("C1")

        assert await asyncio.to_thread(wrote.wait, 10), "compaction write never ran"
        assert (
            seen["ident"] != threading.get_ident()
        ), "compaction ran its atomic_write on the event-loop thread"
        lines = self._read_lines(path)
        assert len(lines) == 5, "oversized inherited file was not bounded on load"

    @staticmethod
    async def _drain_disk_lane():
        """Wait for lane work and any terminal job its loop callback queues."""
        from kiro_crew.executors import channel_history_executor

        sentinel = channel_history_executor().submit(lambda: None)
        await asyncio.to_thread(sentinel.result, 10)
        # A deferred load applies on this loop and may queue a rewrite after
        # the first sentinel. Yield once, then drain that second generation.
        await asyncio.sleep(0)
        sentinel = channel_history_executor().submit(lambda: None)
        await asyncio.to_thread(sentinel.result, 10)

    @pytest.mark.asyncio
    async def test_a_stalled_disk_bounds_the_queue_instead_of_growing_it(
        self, tmp_path, monkeypatch
    ):
        """With the worker wedged, the queue stays bounded and nothing is lost.

        A stalled disk must not let queued closures accumulate one per
        message: the loop thread's submit stays non-blocking, and an append
        past ``_DISK_LANE_MAX_PENDING`` is not queued — a coalesced rewrite
        of the whole window is scheduled instead (one flush submission,
        however many appends are refused). The deque still holds every
        entry, so once the lane drains the file equals the window exactly.
        """
        from kiro_crew import channel_history as ch_mod
        from kiro_crew.executors import channel_history_executor

        cap = 100
        h = ChannelHistory(observe_max_entries=cap, history_dir=tmp_path)
        h.set_observe("C1")

        gate = threading.Event()
        submitted: list[int] = []
        real_executor = channel_history_executor()

        class _CountingExecutor:
            @staticmethod
            def submit(fn, *args, **kwargs):
                submitted.append(1)
                return real_executor.submit(fn, *args, **kwargs)

        # Patch the name channel_history bound at import, not the executors
        # module attribute: the lane calls its own global.
        monkeypatch.setattr(ch_mod, "channel_history_executor", lambda: _CountingExecutor())
        # Wedge the single worker so nothing drains while we flood.
        real_executor.submit(gate.wait, 30)
        total = cap * 3  # crosses the compaction trigger three times
        try:
            for i in range(total):
                h.push("C1", "alice", f"msg{i}")
            # Non-vacuous: a patch that misses the lane's global leaves this
            # list empty and the bound below trivially true.
            assert submitted, "the counting executor never saw a submission"
            # Bounded: at most one queued job per append slot, plus the
            # single coalesced terminal flush the compactions share.
            assert (
                len(submitted) <= ch_mod._DISK_LANE_MAX_PENDING + 2
            ), "a stalled lane accepted more queued jobs than its bound"
        finally:
            gate.set()
        await self._drain_disk_lane()

        # Recaptured: the last coalesced compaction published the whole
        # window, so the dropped appends cost nothing durable — and the
        # appends that DID queue before saturation added no duplicates.
        lines = (tmp_path / "C1.jsonl").read_text(encoding="utf-8").strip().splitlines()
        texts = [json.loads(ln)["text"] for ln in lines]
        assert texts == [
            f"msg{i}" for i in range(total - cap, total)
        ], "the drained file must equal the in-memory window exactly"

    @pytest.mark.asyncio
    async def test_unset_observe_survives_a_saturated_lane(self, tmp_path, monkeypatch):
        """Disabling observe is never droppable, even under saturation.

        The unlink is a coalesced terminal op: it bypasses the append bound,
        supersedes any pending rewrite, and runs after every queued append on
        the single-worker lane — so after the lane drains, the file is gone.
        """
        from kiro_crew import channel_history as ch_mod
        from kiro_crew.executors import channel_history_executor

        h = ChannelHistory(observe_max_entries=1000, history_dir=tmp_path)
        h.set_observe("C1")

        gate = threading.Event()
        real_executor = channel_history_executor()
        real_executor.submit(gate.wait, 30)
        try:
            for i in range(ch_mod._DISK_LANE_MAX_PENDING * 2):
                h.push("C1", "alice", f"msg{i}")
            h.unset_observe("C1")
        finally:
            gate.set()
        await self._drain_disk_lane()

        assert not (
            tmp_path / "C1.jsonl"
        ).exists(), "a saturated lane dropped the unlink — history survived observe-off"

    @pytest.mark.asyncio
    async def test_append_after_compaction_trigger_survives_in_the_file(self, tmp_path):
        """A message pushed right after a cap-triggering push must stay on disk.

        The compaction write is a snapshot taken on the loop thread; if it ran
        unordered against the appends around it, an append landing between the
        snapshot and the rename would be erased by the older snapshot — lost
        across a restart. The single-worker lane makes submission order
        execution order, so the later append lands after the rewrite.
        """
        h = ChannelHistory(observe_max_entries=5, history_dir=tmp_path)
        h.set_observe("C1")
        for i in range(5):
            h.push("C1", "alice", f"msg{i}")  # 5th push queues the compaction
        h.push("C1", "alice", "msg5")  # append submitted after the snapshot

        await self._drain_disk_lane()

        lines = (tmp_path / "C1.jsonl").read_text(encoding="utf-8").strip().splitlines()
        texts = [json.loads(ln)["text"] for ln in lines]
        assert texts == [
            f"msg{i}" for i in range(1, 6)
        ], "the capped replay must retain the append submitted after its snapshot"

    @pytest.mark.asyncio
    async def test_unset_observe_unlink_is_not_overwritten_by_a_queued_write(self, tmp_path):
        """Disabling observe mode must win against writes queued before it."""
        h = ChannelHistory(observe_max_entries=5, history_dir=tmp_path)
        h.set_observe("C1")
        for i in range(5):
            h.push("C1", "alice", f"msg{i}")  # queues appends + a compaction
        h.unset_observe("C1")  # unlink submitted after them

        await self._drain_disk_lane()

        assert not (
            tmp_path / "C1.jsonl"
        ).exists(), "a queued history write resurrected the file unset_observe removed"

    @pytest.mark.asyncio
    async def test_off_loop_terminal_ops_join_the_lane_instead_of_running_inline(self, tmp_path):
        """A terminal op scheduled off the loop must not touch disk inline.

        Inline IO on the calling thread runs concurrently with the lane
        worker, so an older inline unlink could finish after a newer worker
        rewrite (deleting the newest history), and vice versa. The invariant
        is total: every terminal op executes on the single-worker lane, so
        completion order equals schedule order regardless of calling thread.
        Here the worker is wedged; if unset_observe ran its unlink inline,
        the file would vanish while the lane is still blocked.
        """
        from kiro_crew.executors import channel_history_executor

        h = ChannelHistory(observe_max_entries=100, history_dir=tmp_path)
        h.set_observe("C1")
        for i in range(3):
            h.push("C1", "alice", f"msg{i}")
        await self._drain_disk_lane()
        path = tmp_path / "C1.jsonl"
        assert path.exists()

        gate = threading.Event()
        channel_history_executor().submit(gate.wait, 30)  # wedge the worker
        try:
            # Schedule the unlink from a non-loop thread.
            await asyncio.to_thread(h.unset_observe, "C1")
            assert path.exists(), "an off-loop unlink ran inline, bypassing the lane"
        finally:
            gate.set()
        await self._drain_disk_lane()
        assert not path.exists(), "the queued unlink never executed after the lane drained"

    @pytest.mark.asyncio
    async def test_a_failed_compaction_does_not_strand_queued_appends(
        self, tmp_path, monkeypatch, caplog
    ):
        """Appends superseded by a rewrite must still land if that rewrite fails.

        Supersession is publish-gated: an append skips its write only once a
        terminal op scheduled after it has actually PUBLISHED. If the
        compaction's atomic_write raises OSError, nothing was published — the
        queued appends run and their entries reach disk, so a disk error
        costs the coalesced snapshot, never the messages behind it. (The
        REJECTED alternative — supersession decided at schedule time — made
        the appends no-ops the moment the compaction was scheduled, so its
        failure stranded their entries in memory and a crash lost them; this
        test pins why that design was not shipped.)

        The fake mirrors the production call — ``atomic_write(path, content,
        mode=..., parent_dir_fd=...)`` — so the injected failure really is
        the OSError the ``except OSError`` arm handles, and the test asserts
        that arm's own effect (the logged compaction failure): a fake that
        rejected the keywords would surface as a TypeError the arm never
        sees, and the appends assertion below would pass with the arm
        deleted.
        """
        from kiro_crew import channel_history as ch_mod
        from kiro_crew.executors import channel_history_executor

        def _failing_write(path, content, *, mode=None, parent_dir_fd=None):
            raise OSError("disk error injected by test")

        monkeypatch.setattr(ch_mod, "atomic_write", _failing_write)

        h = ChannelHistory(observe_max_entries=5, history_dir=tmp_path)
        h.set_observe("C1")

        gate = threading.Event()
        channel_history_executor().submit(gate.wait, 30)  # wedge the worker
        try:
            # The 5th push queues a compaction behind the queued appends.
            for i in range(5):
                h.push("C1", "alice", f"msg{i}")
        finally:
            gate.set()
        await self._drain_disk_lane()

        lines = (tmp_path / "C1.jsonl").read_text(encoding="utf-8").strip().splitlines()
        texts = [json.loads(ln)["text"] for ln in lines]
        assert texts == [
            f"msg{i}" for i in range(5)
        ], "a failed compaction stranded the appends it superseded"
        failures = [
            record
            for record in caplog.records
            if record.getMessage().startswith("Failed to compact history file")
        ]
        assert len(failures) == 1, "the compaction's OSError was not handled by the except arm"
        assert (
            failures[0].exc_info is not None and failures[0].exc_info[0] is OSError
        ), "the injected failure did not reach the arm as an OSError"

    @pytest.mark.asyncio
    async def test_successful_unlink_clears_failed_rewrite_suppression(self, tmp_path, monkeypatch):
        """Re-observing after a successful unlink resumes append persistence."""
        from kiro_crew import channel_history as ch_mod

        real_atomic_write = ch_mod.atomic_write
        fail_rewrite = True

        def _controlled_write(path, content, *, mode=None, parent_dir_fd=None):
            if fail_rewrite:
                raise OSError("disk full injected by test")
            return real_atomic_write(
                path,
                content,
                mode=mode,
                parent_dir_fd=parent_dir_fd,
            )

        monkeypatch.setattr(ch_mod, "atomic_write", _controlled_write)

        h = ChannelHistory(observe_max_entries=3, history_dir=tmp_path)
        h.set_observe("C1")
        h.push("C1", "alice", "seed")
        await self._drain_disk_lane()
        path = tmp_path / "C1.jsonl"
        assert path.exists()

        h._compact("C1")
        await self._drain_disk_lane()
        assert "C1" in h._rewrite_failed_channels, "the failed rewrite did not arm suppression"

        fail_rewrite = False
        h.unset_observe("C1")
        await self._drain_disk_lane()
        assert not path.exists(), "the successful unlink left the history file behind"

        h.set_observe("C1")
        h.push("C1", "alice", "after re-observe")
        await self._drain_disk_lane()

        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert [json.loads(line)["text"] for line in lines] == [
            "seed",
            "after re-observe",
        ], "re-enable did not restore retained history and resume append persistence"

    @pytest.mark.asyncio
    async def test_failed_rewrite_suppresses_appends_until_recovery(
        self, tmp_path, monkeypatch, caplog
    ):
        """A failed rewrite bounds the log until a later rewrite recovers.

        ``_appends_since_compact`` is reset when the rewrite is SCHEDULED, on
        the deque's mutator thread — the counter's only owner — so a publish
        failure on the lane worker is invisible to it, by design: the next
        compaction is attempted after the next ``observe_max_entries``
        append attempts, the same cadence a successful publish gets. Disk
        appends stop after the first failed rewrite so inode/quota exhaustion
        cannot grow the existing file without bound; the deque keeps moving,
        and the armed count trigger retries the whole-window rewrite. Once a
        rewrite succeeds, ordinary append persistence resumes.
        """
        from kiro_crew import channel_history as ch_mod

        real_atomic_write = ch_mod.atomic_write
        write_succeeds = False

        def _controlled_write(path, content, *, mode=None, parent_dir_fd=None):
            if not write_succeeds:
                raise OSError("disk full injected by test")
            return real_atomic_write(
                path,
                content,
                mode=mode,
                parent_dir_fd=parent_dir_fd,
            )

        monkeypatch.setattr(ch_mod, "atomic_write", _controlled_write)

        def _attempts() -> int:
            return sum(
                1
                for record in caplog.records
                if record.getMessage().startswith("Failed to compact history file")
            )

        h = ChannelHistory(observe_max_entries=3, history_dir=tmp_path)
        h.set_observe("C1")
        for i in range(3):
            h.push("C1", "alice", f"msg{i}")  # the 3rd append schedules a compaction
        await self._drain_disk_lane()
        assert _attempts() == 1, "the count trigger did not fire at the cap"
        path = tmp_path / "C1.jsonl"
        frozen = path.read_text(encoding="utf-8")

        for i in range(3, 5):
            h.push("C1", "alice", f"msg{i}")  # two more: still below the next cap
        await self._drain_disk_lane()
        assert _attempts() == 1, "a failed publish made the trigger fire early"
        assert path.read_text(encoding="utf-8") == frozen
        h.push("C1", "alice", "msg5")  # the cap-th append since the failed attempt
        await self._drain_disk_lane()
        assert _attempts() == 2, "a failed publish disarmed the count trigger"
        assert path.read_text(encoding="utf-8") == frozen
        assert [entry.text for entry in h._channels["C1"]] == ["msg3", "msg4", "msg5"]

        write_succeeds = True
        for i in range(6, 9):
            h.push("C1", "alice", f"msg{i}")
        await self._drain_disk_lane()
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert [json.loads(line)["text"] for line in lines] == ["msg6", "msg7", "msg8"]

        h.push("C1", "alice", "msg9")
        await self._drain_disk_lane()
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert [json.loads(line)["text"] for line in lines] == [
            "msg6",
            "msg7",
            "msg8",
            "msg9",
        ], "disk appends did not resume after a successful rewrite"

    @pytest.mark.asyncio
    async def test_deferred_append_never_recreates_or_retrusts_a_removed_directory(
        self, tmp_path, caplog
    ):
        """Queued and later appends cannot replace a root this instance trusted.

        The deferred worker never creates directories. If the trusted root is
        removed after scheduling, that append fails, and later appends keep
        failing closed rather than blessing a replacement identity. A process
        restart creates a new ChannelHistory trust boundary.
        """
        from kiro_crew.executors import channel_history_executor

        history_dir = tmp_path / "history"
        history_dir.mkdir()
        h = ChannelHistory(observe_max_entries=100, history_dir=history_dir)
        h.set_observe("C1")
        await self._drain_disk_lane()

        gate = threading.Event()
        channel_history_executor().submit(gate.wait, 30)  # wedge the worker
        try:
            h.push("C1", "alice", "queued while dir exists")
            (history_dir / "C1.jsonl").unlink(missing_ok=True)
            history_dir.rmdir()  # removed before the deferred open runs
        finally:
            gate.set()
        await self._drain_disk_lane()

        assert not history_dir.exists(), "the deferred append recreated a removed directory"
        assert h.entry_count("C1") >= 1, "the entry must survive in the deque"

        caplog.clear()
        h.push("C1", "alice", "after root removal")
        await self._drain_disk_lane()
        assert not history_dir.exists(), "a later append trusted a replacement history root"
        assert h.entry_count("C1") == 2, "refused appends must remain recoverable in memory"
        assert any(
            record.getMessage().startswith("Failed to append to history file")
            for record in caplog.records
        ), "an append after root removal failed before reaching the worker"

    @pytest.mark.skipif(not platform_compat.IS_POSIX, reason="POSIX symlink semantics")
    def test_preexisting_in_root_leaf_symlink_is_never_followed(self, tmp_path, caplog):
        """Load, append, rewrite, and unlink keep C1 operations away from linked C2."""
        history_dir = tmp_path / "history"
        history_dir.mkdir()
        c2_path = history_dir / "C2.jsonl"
        original = (
            json.dumps({"user": "bob", "text": "C2 history", "ts": time.time()}) + "\n"
        ).encode()
        c2_path.write_bytes(original)
        c1_path = history_dir / "C1.jsonl"
        c1_path.symlink_to(c2_path.name)

        h = ChannelHistory(observe_max_entries=100, history_dir=history_dir)
        h.set_observe("C1")
        # The READ must refuse the link as well: a symlink at C1's leaf must
        # not load C2's records (or anything outside the root) into C1's window.
        assert h.entry_count("C1") == 0, "set_observe loaded C2's history through C1's linked leaf"
        assert "C2 history" not in h.context_for("C1")
        assert any(
            record.getMessage().startswith("Refusing to read history file")
            for record in caplog.records
        ), "the linked-leaf read was not refused"

        h.push("C1", "alice", "must stay with C1")
        _drain_lane()

        assert c2_path.read_bytes() == original, "the append followed C1's linked leaf into C2"
        assert "C1" in h._load_failed_channels
        assert not any(
            record.getMessage().startswith("Failed to append to history file")
            for record in caplog.records
        ), "a failed load did not suppress the later append"

        h._compact("C1")
        _drain_lane()
        assert c2_path.read_bytes() == original, "the rewrite followed C1's linked leaf into C2"

        h.unset_observe("C1")
        _drain_lane()
        assert c2_path.read_bytes() == original, "the unlink followed C1's linked leaf into C2"
        assert not c1_path.exists() and not c1_path.is_symlink()

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFO semantics")
    def test_load_refuses_a_fifo_at_the_leaf_without_blocking(self, tmp_path, caplog):
        """A FIFO planted at ``<channel>.jsonl`` is refused at once, not read.

        A blocking ``open`` of a FIFO with no writer waits forever, wedging
        the thread that enables observe (gateway boot). The load must open
        non-blocking and refuse anything that is not a regular file.
        """
        history_dir = tmp_path / "history"
        history_dir.mkdir()
        fifo = history_dir / "C1.jsonl"
        os.mkfifo(fifo)

        h = ChannelHistory(observe_max_entries=100, history_dir=history_dir)
        loader = threading.Thread(target=h.set_observe, args=("C1",), daemon=True)
        loader.start()
        loader.join(timeout=5)
        try:
            assert not loader.is_alive(), "set_observe blocked on a FIFO at the history leaf"
        finally:
            if loader.is_alive():
                # Unwedge a blocked reader so it does not outlive the test:
                # a writer open-and-close hands it EOF.
                try:
                    os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
                except OSError:
                    pass
                loader.join(timeout=5)
        assert h.entry_count("C1") == 0
        assert any(
            record.getMessage().startswith("Refusing to read history file")
            for record in caplog.records
        ), "the FIFO at the leaf was not refused"
        assert stat.S_ISFIFO(fifo.lstat().st_mode), "the FIFO must be left in place, untouched"

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFO semantics")
    def test_append_refuses_a_fifo_without_blocking_the_lane(self, tmp_path, caplog):
        """A FIFO append is refused promptly and later channel writes still run."""
        history_dir = tmp_path / "history"
        history_dir.mkdir()
        h = ChannelHistory(observe_max_entries=100, history_dir=history_dir)
        h.set_observe("C1")
        h.set_observe("C2")
        fifo = history_dir / "C1.jsonl"
        os.mkfifo(fifo)

        h.push("C1", "alice", "must not enter the fifo")
        h.push("C2", "bob", "the lane is still live")
        errors: list[BaseException] = []

        def _drain() -> None:
            try:
                _drain_lane()
            except BaseException as exc:
                errors.append(exc)

        drainer = threading.Thread(target=_drain, daemon=True)
        drainer.start()
        drainer.join(timeout=3)
        blocked = drainer.is_alive()
        rescue_fd = -1
        try:
            if blocked:
                # Keep a reader open long enough to release a regressed
                # blocking writer, so the test cannot strand the global lane.
                rescue_fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
                drainer.join(timeout=5)
        finally:
            if rescue_fd != -1:
                os.close(rescue_fd)

        assert not blocked, "the append worker blocked opening a FIFO with no reader"
        assert not drainer.is_alive(), "the history lane stayed wedged after test cleanup"
        assert not errors
        assert "the lane is still live" in (history_dir / "C2.jsonl").read_text(encoding="utf-8")
        assert any(
            record.getMessage().startswith("Failed to append to history file")
            for record in caplog.records
        ), "the FIFO append refusal was not logged"

        # With a reader present the non-blocking open succeeds, so the
        # descriptor-type check itself must still reject the FIFO before write.
        reader_fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        try:
            h.push("C1", "alice", "still must not enter the fifo")
            _drain_lane()
            try:
                written = os.read(reader_fd, 1)
            except BlockingIOError:
                written = b""
            assert written == b"", "the append wrote into a non-regular history leaf"
        finally:
            os.close(reader_fd)

    @pytest.mark.skipif(not platform_compat.IS_POSIX, reason="POSIX openat semantics")
    def test_append_closes_leaf_fd_when_fstat_raises(self, tmp_path, monkeypatch, caplog):
        """A failed post-open ``fstat`` must not leak the leaf descriptor.

        The leaf is opened, then inspected with ``os.fstat`` before it is
        handed to ``os.fdopen``; an ``OSError`` from that inspection is
        logged like any other append failure, and the descriptor opened just
        before it must be closed on that path too — one leaked fd per
        observed message would otherwise exhaust the process.
        """
        from kiro_crew.executors import channel_history_executor

        h = ChannelHistory(observe_max_entries=100, history_dir=tmp_path)
        h.set_observe("C1")
        _drain_lane()  # first trust captured before the probes go in
        leaf_fds: list[int] = []
        futures: list[object] = []
        real_executor = channel_history_executor()
        real_open = os.open
        real_fstat = os.fstat

        class _RecordingExecutor:
            def submit(self, fn, *args, **kwargs):
                future = real_executor.submit(fn, *args, **kwargs)
                futures.append(future)
                return future

        def _open(path, flags, mode=0o777, *, dir_fd=None):
            fd = real_open(path, flags, mode, dir_fd=dir_fd)
            if dir_fd is not None and path == "C1.jsonl":
                leaf_fds.append(fd)
            return fd

        def _fstat(fd):
            if leaf_fds and fd == leaf_fds[-1]:
                raise OSError(errno.EIO, "injected fstat failure")
            return real_fstat(fd)

        recording = _RecordingExecutor()
        with monkeypatch.context() as probes:
            probes.setattr(channel_history, "channel_history_executor", lambda: recording)
            probes.setattr(os, "open", _open)
            probes.setattr(os, "fstat", _fstat)
            with caplog.at_level(logging.WARNING, logger=channel_history.__name__):
                h.push("C1", "alice", "must not leak a descriptor")
                _drain_lane()

        assert len(leaf_fds) == 1, f"expected one leaf open, saw {leaf_fds}"
        assert [future.exception(timeout=10) for future in futures] == [None] * len(futures)
        assert any(
            record.getMessage().startswith("Failed to append to history file")
            for record in caplog.records
        ), "the injected fstat failure was not logged as an append failure"
        with pytest.raises(OSError) as leaked:
            os.fstat(leaf_fds[0])
        assert leaked.value.errno == errno.EBADF, "the leaf descriptor stayed open"

    @pytest.mark.parametrize(
        "channel_id", ["", ".", "..", "C..1", "../C1", "nested/C1", r"nested\C1"]
    )
    def test_observe_path_rejects_non_component_channel_ids(self, tmp_path, channel_id):
        """Observe persistence accepts only one lexical channel-id component."""
        h = ChannelHistory(history_dir=tmp_path)

        assert h._observe_path(channel_id) is None

    def test_history_root_link_planted_after_first_trust_cannot_redirect_io(self, tmp_path, caplog):
        """First-use canonicalisation keeps later root links visible to the pin."""
        history_dir = tmp_path / "history"
        history_dir.mkdir()
        trusted_dir = tmp_path / "trusted-history"
        victim_dir = tmp_path / "victim"
        victim_dir.mkdir()
        victim_file = victim_dir / "C1.jsonl"
        original = (
            json.dumps({"user": "mallory", "text": "victim history", "ts": time.time()}) + "\n"
        )
        victim_file.write_text(original, encoding="utf-8")

        h = ChannelHistory(observe_max_entries=100, history_dir=history_dir)
        h.set_observe("C1")  # first lane use resolves and trusts history_dir
        history_dir.rename(trusted_dir)
        platform_compat.symlink_or_junction(victim_dir, history_dir)
        try:
            h.push("C1", "alice", "must remain in memory")
            _drain_lane()

            assert "victim history" not in h.context_for("C1")
            assert victim_file.read_text(encoding="utf-8") == original
            assert any(
                record.getMessage().startswith("Failed to append to history file")
                for record in caplog.records
            ), "the post-trust root link refusal was not logged"
        finally:
            platform_compat.unlink_link_or_junction(history_dir)
            trusted_dir.rename(history_dir)

    @pytest.mark.asyncio
    async def test_append_refuses_a_symlink_swapped_in_after_scheduling(self, tmp_path):
        """A link swapped in at the history path must not be followed.

        POSIX uses a file symlink to prove the victim file stays unchanged.
        Windows uses an unprivileged directory junction at the same leaf name
        to exercise ``platform_compat.open_append_no_reparse`` against a real
        reparse point. Either way the entry stays recoverable in the deque.
        """
        from kiro_crew.executors import channel_history_executor

        if platform_compat.IS_WINDOWS:
            victim = tmp_path / "victim_dir"
            victim.mkdir()
        else:
            victim = tmp_path / "victim.txt"
            victim.write_text("governance file contents\n", encoding="utf-8")
        history_dir = tmp_path / "history"
        history_dir.mkdir()

        h = ChannelHistory(observe_max_entries=100, history_dir=history_dir)
        h.set_observe("C1")

        gate = threading.Event()
        channel_history_executor().submit(gate.wait, 30)  # wedge the worker
        try:
            # Scheduled while the path is clean: containment passes.
            h.push("C1", "alice", "attacker-controlled message")
            # Swapped in before the deferred open runs.
            link = history_dir / "C1.jsonl"
            if platform_compat.IS_WINDOWS:
                import _winapi

                _winapi.CreateJunction(str(victim), str(link))  # type: ignore[attr-defined]
            else:
                link.symlink_to(victim)
        finally:
            gate.set()
        await self._drain_disk_lane()

        if platform_compat.IS_WINDOWS:
            assert not list(victim.iterdir()), "the deferred append wrote through a junction"
        else:
            assert (
                victim.read_text(encoding="utf-8") == "governance file contents\n"
            ), "the deferred append followed a symlink and wrote through it"
        platform_compat.unlink_link_or_junction(link)
        assert h.entry_count("C1") >= 1, "the entry must survive in the deque"

    @pytest.mark.asyncio
    async def test_append_refuses_an_ancestor_swapped_after_identity_capture(self, tmp_path):
        """The worker must pin the same history-root identity the caller trusted.

        Swapping an ancestor above the history root evades O_NOFOLLOW on the
        root itself: the final component is still a real directory, but it is
        reached through the attacker's link. Comparing the pinned descriptor's
        identity with the caller's captured identity must refuse that write.
        """
        from kiro_crew.executors import channel_history_executor

        trusted_anchor = tmp_path / "trusted"
        history_dir = trusted_anchor / "history"
        history_dir.mkdir(parents=True)
        moved_anchor = tmp_path / "trusted-before-swap"
        victim_anchor = tmp_path / "victim"
        victim_history = victim_anchor / "history"
        victim_history.mkdir(parents=True)

        h = ChannelHistory(observe_max_entries=100, history_dir=history_dir)
        h.set_observe("C1")
        await self._drain_disk_lane()
        captured_identity = h._history_root_identity
        assert captured_identity == h._filesystem_identity(history_dir.stat())

        gate = threading.Event()
        channel_history_executor().submit(gate.wait, 30)
        try:
            h.push("C1", "alice", "must stay in the trusted root")
            trusted_anchor.rename(moved_anchor)
            platform_compat.symlink_or_junction(victim_anchor, trusted_anchor)
        finally:
            gate.set()
        try:
            await self._drain_disk_lane()
            assert not (
                victim_history / "C1.jsonl"
            ).exists(), "the deferred append followed a swapped ancestor"
            assert h.entry_count("C1") >= 1, "the refused entry must remain recoverable"
        finally:
            platform_compat.unlink_link_or_junction(trusted_anchor)
            moved_anchor.rename(trusted_anchor)

    @pytest.mark.asyncio
    async def test_append_refuses_a_parent_directory_swapped_for_a_link(self, tmp_path):
        """A parent directory swapped for a symlink must not redirect the write.

        The leaf guard alone does not cover this: a real (non-link) file
        opened through a linked PARENT lands outside the history dir. The
        worker pins the parent with ``platform_compat.pin_directory`` (on
        POSIX an ``O_DIRECTORY | O_NOFOLLOW`` open of the directory itself —
        a directory swapped for a link after scheduling fails that open
        outright) and resolves the leaf against the held descriptor, so
        nothing re-walks the full path. The victim directory must stay empty
        and the entry stays in the deque.
        """
        from kiro_crew.executors import channel_history_executor

        victim_dir = tmp_path / "victim_dir"
        victim_dir.mkdir()
        history_dir = tmp_path / "history"
        history_dir.mkdir()

        h = ChannelHistory(observe_max_entries=100, history_dir=history_dir)
        h.set_observe("C1")
        await self._drain_disk_lane()

        gate = threading.Event()
        channel_history_executor().submit(gate.wait, 30)  # wedge the worker
        try:
            # Scheduled while the parent is a real directory.
            h.push("C1", "alice", "attacker-controlled message")
            # Swap the PARENT for a link before the deferred open runs.
            (history_dir / "C1.jsonl").unlink(missing_ok=True)
            history_dir.rmdir()
            platform_compat.symlink_or_junction(victim_dir, history_dir)
        finally:
            gate.set()
        await self._drain_disk_lane()

        assert not (
            victim_dir / "C1.jsonl"
        ).exists(), "the deferred append wrote through a linked parent directory"
        platform_compat.unlink_link_or_junction(history_dir)
        assert h.entry_count("C1") >= 1, "the entry must survive in the deque"

    @pytest.mark.asyncio
    async def test_terminal_rewrite_refuses_a_parent_directory_swapped_for_a_link(self, tmp_path):
        """A compaction rewrite must not follow a linked history directory.

        Terminal ops run deferred like the appends, so they hold the same
        parent pin. The history directory is replaced by a link to a victim
        directory after the rewrite is scheduled; the pin refuses it, the
        victim directory stays empty, and nothing is published.
        """
        from kiro_crew.executors import channel_history_executor

        victim_dir = tmp_path / "victim_dir"
        victim_dir.mkdir()
        history_dir = tmp_path / "history"
        history_dir.mkdir()

        h = ChannelHistory(observe_max_entries=100, history_dir=history_dir)
        h.set_observe("C1")
        await self._drain_disk_lane()

        gate = threading.Event()
        channel_history_executor().submit(gate.wait, 30)  # wedge the worker
        try:
            h.push("C1", "alice", "attacker-controlled message")
            h._compact("C1")  # terminal rewrite scheduled while the dir is real
            (history_dir / "C1.jsonl").unlink(missing_ok=True)
            history_dir.rmdir()
            platform_compat.symlink_or_junction(victim_dir, history_dir)
        finally:
            gate.set()
        await self._drain_disk_lane()

        assert not (
            victim_dir / "C1.jsonl"
        ).exists(), "the terminal rewrite wrote through a linked parent directory"
        platform_compat.unlink_link_or_junction(history_dir)

    @pytest.mark.asyncio
    async def test_a_saturated_append_reaches_disk_via_the_coalesced_rewrite(
        self, tmp_path, monkeypatch
    ):
        """An append refused by a full lane must still reach disk.

        Saturation schedules a coalesced rewrite of the window (never
        dropped) instead of relying on the count-triggered compaction, which
        may be hundreds of appends away. Once the lane recovers, the file
        holds the refused entry — with a cap far above the message count, so
        only the saturation path could have published it.
        """
        from kiro_crew.executors import channel_history_executor

        monkeypatch.setattr(channel_history, "_DISK_LANE_MAX_PENDING", 2)
        h = ChannelHistory(observe_max_entries=1000, history_dir=tmp_path)
        h._disk_lane_slots = threading.Semaphore(2)
        h.set_observe("C1")

        gate = threading.Event()
        channel_history_executor().submit(gate.wait, 30)  # wedge the worker
        try:
            for i in range(3):  # third push saturates the 2-slot lane
                h.push("C1", "alice", f"msg{i}")
        finally:
            gate.set()
        await self._drain_disk_lane()

        lines = (tmp_path / "C1.jsonl").read_text(encoding="utf-8").strip().splitlines()
        texts = [json.loads(ln)["text"] for ln in lines]
        assert texts == [
            "msg0",
            "msg1",
            "msg2",
        ], "the saturated entry must be published by the coalesced rewrite"

    @pytest.mark.asyncio
    async def test_reenable_load_failure_never_rewrites_a_reduced_window(
        self, tmp_path, monkeypatch
    ):
        """A failed reload must not publish memory that is a subset of disk.

        The queued UNLINK is still cancelled: if it had already begun when
        the read failed, its user-requested deletion may complete, but a
        surviving full file must never be truncated to the downsized buffer.
        """
        from kiro_crew.executors import channel_history_executor

        path = tmp_path / "C1.jsonl"
        now = time.time()
        records = [
            {"user": "alice", "text": f"msg{i}", "msg_ts": str(i), "ts": now} for i in range(5)
        ]
        path.write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )
        h = ChannelHistory(max_entries=2, observe_max_entries=5, history_dir=tmp_path)
        h.set_observe("C1")
        await self._drain_disk_lane()
        assert h.entry_count("C1") == 5

        # ``_read_observe`` opens the leaf through ``_open_observe_file``
        # (pinned dirfd + openat), never ``Path.open``: fail THAT, and keep
        # the failure installed until the lane has drained — the wedged
        # worker runs the deferred read only after ``release.set()``.
        monkeypatch.setattr(
            h,
            "_open_observe_file",
            lambda path, root_identity: (_ for _ in ()).throw(
                OSError("transient history read failure")
            ),
        )
        release = threading.Event()
        channel_history_executor().submit(release.wait)
        try:
            h.unset_observe("C1")
            assert h.entry_count("C1") == 2
            h.set_observe("C1")
        finally:
            release.set()
        await self._drain_disk_lane()

        assert "C1" in h._load_failed_channels, "the failed reload did not arm suppression"
        persisted = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        assert [entry["text"] for entry in persisted] == [f"msg{i}" for i in range(5)]

    @pytest.mark.asyncio
    async def test_reenable_success_rewrites_the_complete_bounded_window(self, tmp_path):
        """A successful reload may compact because disk and memory were merged."""
        from kiro_crew.executors import channel_history_executor

        path = tmp_path / "C1.jsonl"
        now = time.time()
        records = [
            {"user": "alice", "text": f"msg{i}", "msg_ts": str(i), "ts": now} for i in range(5)
        ]
        path.write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )
        h = ChannelHistory(max_entries=2, observe_max_entries=5, history_dir=tmp_path)
        h.set_observe("C1")
        await self._drain_disk_lane()

        release = threading.Event()
        channel_history_executor().submit(release.wait)
        try:
            h.unset_observe("C1")
            h.set_observe("C1")
        finally:
            release.set()
        await self._drain_disk_lane()

        persisted = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        assert [entry["text"] for entry in persisted] == [f"msg{i}" for i in range(5)]

    @pytest.mark.asyncio
    async def test_terminal_rewrite_refuses_an_ancestor_swapped_after_scheduling(
        self, tmp_path, caplog
    ):
        """A terminal op must pin the same history root scheduled by its caller."""
        from kiro_crew.executors import channel_history_executor

        trusted_anchor = tmp_path / "trusted"
        history_dir = trusted_anchor / "history"
        history_dir.mkdir(parents=True)
        moved_anchor = tmp_path / "trusted-before-swap"
        victim_anchor = tmp_path / "victim"
        victim_history = victim_anchor / "history"
        victim_history.mkdir(parents=True)

        h = ChannelHistory(observe_max_entries=100, history_dir=history_dir)
        h.push("C1", "alice", "must stay in the trusted root")
        h.set_observe("C1")
        await self._drain_disk_lane()
        h._channels["C1"][0].wall_ts = time.time()

        gate = threading.Event()
        channel_history_executor().submit(gate.wait, 30)
        try:
            h._compact("C1")
            trusted_anchor.rename(moved_anchor)
            platform_compat.symlink_or_junction(victim_anchor, trusted_anchor)
        finally:
            gate.set()
        try:
            await self._drain_disk_lane()
            assert not (
                victim_history / "C1.jsonl"
            ).exists(), "the terminal rewrite followed a swapped ancestor"
            assert any(
                "history directory identity changed" in record.getMessage()
                for record in caplog.records
            ), "the refused terminal rewrite was not logged"
        finally:
            platform_compat.unlink_link_or_junction(trusted_anchor)
            moved_anchor.rename(trusted_anchor)

    @pytest.mark.asyncio
    async def test_reenabling_after_unlink_persists_the_retained_window(self, tmp_path):
        """A completed observe-off unlink must not strand retained history in memory.

        This toggle is intentionally unwedged: the lane drains after observe-off,
        proving the file is absent before observe is re-enabled. The successful
        absent-file load must republish the retained persistable window so a fresh
        process can recover it without waiting for another cap of appends.
        """
        path = tmp_path / "C1.jsonl"
        h = ChannelHistory(observe_max_entries=100, history_dir=tmp_path)
        h.set_observe("C1")
        h.push("C1", "alice", "survives a restart", msg_ts="1")
        await self._drain_disk_lane()
        assert path.exists()

        h.unset_observe("C1")
        await self._drain_disk_lane()
        assert not path.exists(), "the regression must exercise the absent-file load"

        h.set_observe("C1")
        await self._drain_disk_lane()
        assert path.exists(), "re-enable left the retained window only in memory"

        fresh = ChannelHistory(observe_max_entries=100, history_dir=tmp_path)
        fresh.set_observe("C1")
        await self._drain_disk_lane()
        assert [entry.text for entry in fresh._channels["C1"]] == ["survives a restart"]

    @pytest.mark.asyncio
    async def test_reenabling_observe_supersedes_a_queued_unlink(self, tmp_path):
        """An observe off-then-on toggle must not lose the reloaded history.

        ``unset_observe`` queues a coalesced UNLINK. ``set_observe`` queues a
        reload after it on the same lane; without a superseding terminal op,
        the unlink removes exactly the history that reload brings back.
        ``set_observe`` therefore publishes the reloaded window as a REWRITE —
        last-writer-wins over the earlier unlink.
        """
        from kiro_crew.executors import channel_history_executor

        h = ChannelHistory(observe_max_entries=100, history_dir=tmp_path)
        h.set_observe("C1")
        h.push("C1", "alice", "precious history")
        await self._drain_disk_lane()
        assert "precious history" in (tmp_path / "C1.jsonl").read_text(encoding="utf-8")

        # Wedge the single-worker lane so the toggle's UNLINK stays queued
        # while set_observe reloads the file.
        release = threading.Event()
        channel_history_executor().submit(release.wait)
        try:
            h.unset_observe("C1")  # queues the coalesced UNLINK
            h.set_observe("C1")  # reloads the file; must supersede the unlink
        finally:
            release.set()
        await self._drain_disk_lane()

        assert (tmp_path / "C1.jsonl").exists(), "the stale unlink deleted the reloaded history"
        text = (tmp_path / "C1.jsonl").read_text(encoding="utf-8")
        assert "precious history" in text, "the superseding rewrite lost the reloaded entries"

    @pytest.mark.asyncio
    async def test_rapid_observe_toggle_does_not_duplicate_history(self, tmp_path):
        """An off-then-on toggle must not persist duplicated entries.

        ``unset_observe`` keeps the newest window in memory for ordinary
        context; ``_load_observe`` dedupes the disk/memory merge by message
        identity, so the re-enable REWRITE publishes exactly one copy of
        each entry.
        """
        from kiro_crew.executors import channel_history_executor

        h = ChannelHistory(observe_max_entries=100, history_dir=tmp_path)
        h.set_observe("C1")
        h.push("C1", "alice", "one of a kind")
        await self._drain_disk_lane()

        # Wedge the lane so the toggle's UNLINK and deferred reload remain
        # ordered and pending together.
        release = threading.Event()
        channel_history_executor().submit(release.wait)
        try:
            h.unset_observe("C1")
            h.set_observe("C1")
        finally:
            release.set()
        await self._drain_disk_lane()

        text = (tmp_path / "C1.jsonl").read_text(encoding="utf-8")
        assert text.count("one of a kind") == 1, "the toggle duplicated persisted history"
        assert h.entry_count("C1") == 1, "the toggle duplicated the in-memory window"

    @pytest.mark.asyncio
    async def test_distinct_tsless_messages_survive_the_merge_dedupe(self, tmp_path):
        """Two ts-less messages sharing author/text/thread are NOT collapsed.

        The fallback identity includes ``wall_ts``, so two distinct messages
        that happen to repeat the same text stay two entries; only a true
        reload of one persisted entry (identical persisted ``ts``) dedupes.
        """
        now = time.time()
        path = tmp_path / "C1.jsonl"
        with path.open("w", encoding="utf-8") as f:
            for offset in (2.0, 1.0):
                f.write(
                    json.dumps(
                        {
                            "user": "alice",
                            "text": "same words",
                            "thread_ts": None,
                            "ts": now - offset,
                        }
                    )
                    + "\n"
                )

        h = ChannelHistory(observe_max_entries=100, history_dir=tmp_path)
        h.set_observe("C1")
        await self._drain_disk_lane()
        assert h.entry_count("C1") == 2, "distinct ts-less messages were collapsed by the dedupe"

    @pytest.mark.asyncio
    async def test_reenable_with_a_failed_load_still_cancels_the_queued_unlink(
        self, tmp_path, monkeypatch
    ):
        """A read failure on re-enable must not leave the stale unlink armed.

        ``set_observe`` cancels a queued UNLINK BEFORE the load, so the
        cancel cannot depend on the load succeeding — a transient read
        failure (fd exhaustion, mount blip) otherwise leaves the pending
        unlink to delete a file that still holds valid entries.
        """
        from kiro_crew.executors import channel_history_executor

        h = ChannelHistory(observe_max_entries=100, history_dir=tmp_path)
        h.set_observe("C1")
        h.push("C1", "alice", "survives the blip")
        await self._drain_disk_lane()
        assert "survives the blip" in (tmp_path / "C1.jsonl").read_text(encoding="utf-8")

        release = threading.Event()
        channel_history_executor().submit(release.wait)
        try:
            h.unset_observe("C1")  # queues the coalesced UNLINK
            # Simulate the load failing: drop the in-memory window and make
            # the read raise, so nothing repopulates the deque either.
            h._channels.pop("C1", None)
            monkeypatch.setattr(ChannelHistory, "_load_observe", lambda self, cid: None)
            h.set_observe("C1")  # must cancel the queued unlink regardless
        finally:
            release.set()
        await self._drain_disk_lane()

        assert (
            tmp_path / "C1.jsonl"
        ).exists(), "a failed load on re-enable left the queued unlink to delete the file"
        assert "survives the blip" in (tmp_path / "C1.jsonl").read_text(encoding="utf-8")

    @pytest.mark.asyncio
    async def test_boot_time_set_observe_schedules_no_rewrite(self, tmp_path):
        """Enabling observe at startup must not queue data-scaled disk work.

        Gateway boot calls ``set_observe`` for every persisted observe channel
        before the dashboard binds. No unlink can be in flight in a fresh
        process, so the toggle-supersede REWRITE stays off the boot path: it
        fires only when a terminal op was scheduled in this process (a real
        off-then-on transition).
        """
        seed = ChannelHistory(observe_max_entries=100, history_dir=tmp_path)
        seed.set_observe("C1")
        seed.push("C1", "alice", "persisted before restart")
        await self._drain_disk_lane()

        fresh = ChannelHistory(observe_max_entries=100, history_dir=tmp_path)
        fresh.set_observe("C1")  # the boot-path call
        await self._drain_disk_lane()
        assert not fresh._pending_terminal, "boot-time set_observe queued a terminal op"
        assert fresh.entry_count("C1") == 1, "the boot-path load itself must still run"

    def test_open_append_no_reparse_refuses_a_link_or_junction_and_appends_to_a_file(
        self, tmp_path
    ):
        """The no-follow append open refuses a real link atomically.

        POSIX exercises a file symlink; Windows exercises the unprivileged
        directory-junction reparse shape. A real file must append on both.
        """
        real = tmp_path / "real.jsonl"
        fd = platform_compat.open_append_no_reparse(real)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write("one\n")
        fd = platform_compat.open_append_no_reparse(real)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write("two\n")
        assert real.read_text(encoding="utf-8") == "one\ntwo\n"

        link = tmp_path / "link.jsonl"
        if platform_compat.IS_WINDOWS:
            import _winapi

            victim = tmp_path / "victim_dir"
            victim.mkdir()
            _winapi.CreateJunction(str(victim), str(link))  # type: ignore[attr-defined]
        else:
            victim = tmp_path / "victim.txt"
            victim.write_text("secret\n", encoding="utf-8")
            link.symlink_to(victim)
        with pytest.raises(OSError):
            platform_compat.open_append_no_reparse(link)
        if platform_compat.IS_WINDOWS:
            assert not list(victim.iterdir())
        else:
            assert victim.read_text(encoding="utf-8") == "secret\n"
        platform_compat.unlink_link_or_junction(link)

    def test_windows_append_open_requests_writable_binary_crt_flags(self, tmp_path, monkeypatch):
        """The Windows CRT descriptor must be writable, append-only, and binary."""
        binary_flag = 0x8000
        seen: dict[str, int] = {}
        fake_fd = os.open(tmp_path / "fake-handle.jsonl", os.O_WRONLY | os.O_CREAT, 0o600)

        def _recording_open(path, **kwargs):
            seen.update(kwargs)
            return fake_fd

        monkeypatch.setattr(platform_compat, "IS_POSIX", False)
        monkeypatch.setattr(os, "O_BINARY", binary_flag, raising=False)
        monkeypatch.setattr(platform_compat, "_win_open_without_following", _recording_open)
        fd = platform_compat.open_append_no_reparse(tmp_path / "history.jsonl")
        try:
            assert fd == fake_fd
            crt_flags = seen["crt_flags"]
            accmode = getattr(os, "O_ACCMODE", os.O_RDONLY | os.O_WRONLY | os.O_RDWR)
            assert crt_flags & accmode == os.O_WRONLY
            assert crt_flags & os.O_APPEND
            assert crt_flags & binary_flag
        finally:
            os.close(fd)
