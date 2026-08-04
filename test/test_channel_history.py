"""Tests for channel history buffer."""

from __future__ import annotations

import json
import time

from kiro_crew.channel_history import ChannelHistory


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

        path = tmp_path / "C1.jsonl"
        assert path.exists()
        lines = path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["user"] == "alice"
        assert data["text"] == "hello world"
        assert data["ts"] is not None

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

        # File should be compacted — only the recent entry remains
        lines = path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["user"] == "bob"

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

        path = tmp_path / "C1.jsonl"
        assert path.exists()

        h.unset_observe("C1")
        assert not path.exists()

    def test_thread_ts_persisted(self, tmp_path):
        """thread_ts is round-tripped through JSONL."""
        h = ChannelHistory(history_dir=tmp_path)
        h.set_observe("C1")
        h.push("C1", "alice", "reply", thread_ts="T1")

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

        # The user text must appear in the assembled message.  Other context
        # lines (e.g. [RESOURCES] advisory on memory-pressured hosts) may be
        # prepended, so we assert containment rather than startswith.
        assert "hello" in msg
