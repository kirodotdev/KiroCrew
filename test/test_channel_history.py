"""Tests for channel history buffer."""

from __future__ import annotations

import asyncio
import collections
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

    All observe-file IO — appends, rotations, loads and unlinks alike —
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

    def test_load_observe_reads_the_older_generation_before_the_live_file(self, tmp_path):
        """Both generations load, oldest first, so the window is in message order."""
        now = time.time()
        older = tmp_path / "C1.jsonl.1"
        older.write_text(
            "".join(
                json.dumps({"user": "alice", "text": f"old{i}", "ts": now - 10 + i}) + "\n"
                for i in range(3)
            ),
            encoding="utf-8",
        )
        live = tmp_path / "C1.jsonl"
        live.write_text(
            "".join(
                json.dumps({"user": "alice", "text": f"live{i}", "ts": now - 5 + i}) + "\n"
                for i in range(2)
            ),
            encoding="utf-8",
        )

        h = ChannelHistory(history_dir=tmp_path)
        h.set_observe("C1")

        assert [e.text for e in h._channels["C1"]] == ["old0", "old1", "old2", "live0", "live1"]

    def test_load_observe_keeps_newest_entries_without_rewriting(self, tmp_path):
        """An oversized single file is capped in memory and left byte-for-byte intact."""
        path = tmp_path / "C1.jsonl"
        now = time.time()
        entries = [
            {"user": "alice", "text": f"msg{index}", "msg_ts": str(index), "ts": now}
            for index in range(5)
        ]
        original = "".join(json.dumps(entry) + "\n" for entry in entries)
        path.write_text(original, encoding="utf-8")

        h = ChannelHistory(history_dir=tmp_path, observe_max_entries=3)
        h.set_observe("C1")
        _drain_lane()

        assert [entry.text for entry in h._channels["C1"]] == ["msg2", "msg3", "msg4"]
        # The inherited file is rotated aside whole, never re-serialized.
        assert not path.exists()
        assert (tmp_path / "C1.jsonl.1").read_text(encoding="utf-8") == original

    def test_load_observe_bounds_fields_in_memory_and_leaves_the_file_alone(self, tmp_path):
        """Legacy fields are bounded in memory; the file is never normalized on disk."""
        path = tmp_path / "C1.jsonl"
        oversized_user = "u" * (channel_history.HISTORY_MAX_ID_CHARS + 1)
        oversized_text = "x" * (channel_history.HISTORY_MAX_TEXT_CHARS + 1)
        oversized_thread_ts = "t" * (channel_history.HISTORY_MAX_ID_CHARS + 1)
        oversized_msg_ts = "m" * (channel_history.HISTORY_MAX_ID_CHARS + 1)
        original = (
            json.dumps(
                {
                    "user": oversized_user,
                    "text": oversized_text,
                    "thread_ts": oversized_thread_ts,
                    "msg_ts": oversized_msg_ts,
                    "ts": time.time(),
                }
            )
            + "\n"
        )
        path.write_text(original, encoding="utf-8")

        h = ChannelHistory(history_dir=tmp_path)
        h.set_observe("C1")
        _drain_lane()

        entry = h._channels["C1"][0]
        assert entry.user == oversized_user[: channel_history.HISTORY_MAX_ID_CHARS]
        assert entry.text == oversized_text[: channel_history.HISTORY_MAX_TEXT_CHARS]
        assert entry.thread_ts == oversized_thread_ts[: channel_history.HISTORY_MAX_ID_CHARS]
        assert entry.msg_ts == oversized_msg_ts[: channel_history.HISTORY_MAX_ID_CHARS]
        assert path.read_text(encoding="utf-8") == original

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

    def test_a_generation_with_one_fresh_record_among_expired_ones_is_kept_whole(self, tmp_path):
        """TTL filters the window only; a generation with any fresh record is never trimmed."""
        now = time.time()
        older = tmp_path / "C1.jsonl.1"
        older_original = "".join(
            json.dumps({"user": "alice", "text": text, "ts": ts}) + "\n"
            for text, ts in (
                ("old0", now - 700000),
                ("old1", now - 690000),
                ("old-fresh", now - 20),
                ("old2", now - 680000),
            )
        )
        older.write_text(older_original, encoding="utf-8")
        path = tmp_path / "C1.jsonl"
        original = "".join(
            json.dumps({"user": "bob", "text": text, "ts": ts}) + "\n"
            for text, ts in (("old", now - 700000), ("recent", now - 10))
        )
        path.write_text(original, encoding="utf-8")

        h = ChannelHistory(history_dir=tmp_path)
        h.set_observe("C1")
        _drain_lane()

        assert [entry.text for entry in h._channels["C1"]] == ["old-fresh", "recent"]
        assert older.read_text(encoding="utf-8") == older_original
        assert path.read_text(encoding="utf-8") == original

    def test_a_quiet_channel_with_only_expired_records_has_both_generations_removed(self, tmp_path):
        """Both generations are unlinked on load once every record is past the TTL."""
        now = time.time()
        older = tmp_path / "C1.jsonl.1"
        older.write_text(
            "".join(
                json.dumps({"user": "alice", "text": f"old{i}", "ts": now - 700000 + i}) + "\n"
                for i in range(3)
            ),
            encoding="utf-8",
        )
        path = tmp_path / "C1.jsonl"
        path.write_text(
            "".join(
                json.dumps({"user": "alice", "text": f"live{i}", "ts": now - 650000 + i}) + "\n"
                for i in range(2)
            ),
            encoding="utf-8",
        )

        h = ChannelHistory(history_dir=tmp_path)
        h.set_observe("C1")
        _drain_lane()

        assert not older.exists(), "the fully expired older generation was kept"
        assert not path.exists(), "the fully expired live file was kept"
        assert h.entry_count("C1") == 0

        # The lane's record count follows the removal: the next push starts a
        # fresh live file holding just that record.
        h.push("C1", "bob", "fresh")
        _drain_lane()
        assert [json.loads(line)["text"] for line in path.read_text().splitlines()] == ["fresh"]
        assert not older.exists()

    def test_a_fully_expired_older_generation_is_removed_while_a_fresh_live_file_is_kept(
        self, tmp_path
    ):
        """Only the generation with nothing inside the TTL is removed; the other stays whole."""
        now = time.time()
        older = tmp_path / "C1.jsonl.1"
        older.write_text(
            "".join(
                json.dumps({"user": "alice", "text": f"old{i}", "ts": now - 700000 + i}) + "\n"
                for i in range(3)
            ),
            encoding="utf-8",
        )
        path = tmp_path / "C1.jsonl"
        original = "".join(
            json.dumps({"user": "bob", "text": text, "ts": ts}) + "\n"
            for text, ts in (("fresh0", now - 15), ("fresh1", now - 5))
        )
        path.write_text(original, encoding="utf-8")

        h = ChannelHistory(history_dir=tmp_path)
        h.set_observe("C1")
        _drain_lane()

        assert not older.exists(), "the fully expired older generation was kept"
        assert path.read_text(encoding="utf-8") == original, "the live file was touched"
        assert [entry.text for entry in h._channels["C1"]] == ["fresh0", "fresh1"]

    def test_a_slow_channel_live_file_with_an_expired_head_is_rotated_aside_on_load(self, tmp_path):
        """A live file whose oldest record is past the TTL, with no ``.1``, is moved aside whole.

        A slow channel never fills the cap, so without this its expired
        records would stay on disk until it did. The rename deletes nothing
        (there is no ``.1`` to replace), the window keeps the fresh records,
        and a later load removes the moved-aside generation once its newest
        record has expired too.
        """
        now = time.time()
        path = tmp_path / "C1.jsonl"
        older = tmp_path / "C1.jsonl.1"
        original = "".join(
            json.dumps({"user": "alice", "text": text, "ts": ts}) + "\n"
            for text, ts in (("aged0", now - 700000), ("aged1", now - 690000), ("fresh", now - 10))
        )
        path.write_text(original, encoding="utf-8")

        h = ChannelHistory(history_dir=tmp_path)
        h.set_observe("C1")
        _drain_lane()

        assert not path.exists(), "the live file with an expired head was left in place"
        assert older.read_text(encoding="utf-8") == original, "the rotated generation was altered"
        assert [entry.text for entry in h._channels["C1"]] == ["fresh"]
        assert h._live_records["C1"] == 0

        # The next push starts a fresh live file; both generations then load.
        h.push("C1", "bob", "next")
        _drain_lane()
        assert [json.loads(line)["text"] for line in path.read_text().splitlines()] == ["next"]
        reloaded = ChannelHistory(history_dir=tmp_path)
        reloaded.set_observe("C1")
        _drain_lane()
        assert [entry.text for entry in reloaded._channels["C1"]] == ["fresh", "next"]
        assert older.read_text(encoding="utf-8") == original

        # Once every record in the moved-aside generation is past the TTL, a
        # load removes it; the live file, still fresh, is untouched.
        later = ChannelHistory(observe_ttl_secs=5, history_dir=tmp_path)
        later.set_observe("C1")
        _drain_lane()
        assert not older.exists(), "the aged-out generation was not removed"
        assert [json.loads(line)["text"] for line in path.read_text().splitlines()] == ["next"]
        assert [entry.text for entry in later._channels["C1"]] == ["next"]

    def test_an_expired_head_never_rotates_over_an_older_generation_with_a_fresh_record(
        self, tmp_path
    ):
        """The age-driven rotation only ever moves the live file into an empty ``.1`` slot.

        A ``.1`` that still holds a fresh record beside a live file whose
        oldest record is expired can only arise from a clock anomaly; nothing
        is renamed or removed, so nothing is lost.
        """
        now = time.time()
        older = tmp_path / "C1.jsonl.1"
        older_original = "".join(
            json.dumps({"user": "alice", "text": text, "ts": ts}) + "\n"
            for text, ts in (("old0", now - 700000), ("old-fresh", now - 20))
        )
        older.write_text(older_original, encoding="utf-8")
        path = tmp_path / "C1.jsonl"
        original = "".join(
            json.dumps({"user": "bob", "text": text, "ts": ts}) + "\n"
            for text, ts in (("aged", now - 700000), ("fresh", now - 10))
        )
        path.write_text(original, encoding="utf-8")

        h = ChannelHistory(history_dir=tmp_path)
        h.set_observe("C1")
        _drain_lane()

        assert older.read_text(encoding="utf-8") == older_original
        assert path.read_text(encoding="utf-8") == original
        assert sorted(p.name for p in tmp_path.iterdir()) == ["C1.jsonl", "C1.jsonl.1"]
        assert [entry.text for entry in h._channels["C1"]] == ["old-fresh", "fresh"]
        assert h._live_records["C1"] == 2

    def test_a_live_file_whose_oldest_record_is_inside_the_ttl_is_not_moved(self, tmp_path):
        """No ``.1`` and every live record inside the TTL: the load touches nothing."""
        now = time.time()
        path = tmp_path / "C1.jsonl"
        original = "".join(
            json.dumps({"user": "alice", "text": text, "ts": ts}) + "\n"
            for text, ts in (("older-but-fresh", now - 3600), ("fresh", now - 10))
        )
        path.write_text(original, encoding="utf-8")

        h = ChannelHistory(history_dir=tmp_path)
        h.set_observe("C1")
        _drain_lane()

        assert path.read_text(encoding="utf-8") == original
        assert not (tmp_path / "C1.jsonl.1").exists()
        assert [entry.text for entry in h._channels["C1"]] == ["older-but-fresh", "fresh"]
        assert h._live_records["C1"] == 2

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

    def test_unset_observe_removes_both_generations(self, tmp_path):
        """unset_observe deletes the live file and the older generation."""
        h = ChannelHistory(observe_max_entries=2, history_dir=tmp_path)
        h.set_observe("C1")
        for index in range(3):
            h.push("C1", "alice", f"msg{index}")
        _drain_lane()

        live = tmp_path / "C1.jsonl"
        older = tmp_path / "C1.jsonl.1"
        assert live.exists() and older.exists()

        h.unset_observe("C1")
        _drain_lane()
        assert not live.exists()
        assert not older.exists()

    def test_unset_observe_removes_the_older_generation_when_the_live_unlink_fails(
        self, tmp_path, monkeypatch, caplog
    ):
        """Each generation is removed independently on observe-off.

        A refused unlink of the live file (a Windows sharing violation from an
        AV or indexer handle, say) is logged for that leaf and must not leave
        the older generation's persisted messages behind.
        """
        h = ChannelHistory(observe_max_entries=2, history_dir=tmp_path)
        h.set_observe("C1")
        for index in range(3):
            h.push("C1", "alice", f"msg{index}")
        _drain_lane()

        live = tmp_path / "C1.jsonl"
        older = tmp_path / "C1.jsonl.1"
        assert live.exists() and older.exists()

        real_unlink = os.unlink

        # ``os.unlink`` is what both removal paths reach: the dir_fd path calls
        # it with the bare name, and ``Path.unlink`` calls it with the full path.
        def _refuse_live(target, *args, **kwargs):
            if os.path.basename(os.fspath(target)) == "C1.jsonl":
                raise PermissionError(errno.EACCES, "unlink refused by test")
            return real_unlink(target, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(os, "unlink", _refuse_live)
            h.unset_observe("C1")
            _drain_lane()
        assert os.unlink is real_unlink

        assert not older.exists(), "a refused live unlink left the older generation on disk"
        assert live.exists(), "the refusal was not exercised"
        assert any(
            record.levelno == logging.WARNING
            and record.getMessage() == f"Failed to remove history file {live}"
            for record in caplog.records
        ), "the refused unlink was not logged for the live leaf"

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
    """The persisted observe history must honour ``observe_max_entries``.

    The file is bounded by ROTATION: once the live file holds a cap of
    records, the lane renames it to the ``.1`` generation and the next append
    starts a fresh live file. No record is ever re-serialized, so every byte
    on disk is exactly what an append wrote, and the two generations together
    hold at most about twice the cap while never fewer than the newest full
    generation.
    """

    @staticmethod
    def _lines(tmp_path, channel="C1", generation=""):
        path = tmp_path / f"{channel}.jsonl{generation}"
        if not path.exists():
            return []
        return path.read_text(encoding="utf-8").strip().splitlines()

    @classmethod
    def _texts(cls, tmp_path, channel="C1", generation=""):
        return [json.loads(line)["text"] for line in cls._lines(tmp_path, channel, generation)]

    @classmethod
    def _union(cls, tmp_path, channel="C1"):
        """Every persisted record, older generation first."""
        return cls._texts(tmp_path, channel, ".1") + cls._texts(tmp_path, channel)

    @staticmethod
    def _write_records(path, texts, ts=None):
        ts = time.time() if ts is None else ts
        content = "".join(
            json.dumps({"user": "alice", "text": text, "msg_ts": text, "ts": ts}) + "\n"
            for text in texts
        )
        path.write_text(content, encoding="utf-8")
        return content

    def test_load_skips_oversized_record_with_bounded_reads(self, tmp_path, monkeypatch, caplog):
        """An oversized record is skipped without materialising its whole line."""
        record_cap = (
            6 * (channel_history.HISTORY_MAX_TEXT_CHARS + 3 * channel_history.HISTORY_MAX_ID_CHARS)
            + 1024
        )
        path = tmp_path / "C1.jsonl"
        now = time.time()
        before = json.dumps({"user": "alice", "text": "before", "ts": now}).encode()
        after = json.dumps({"user": "bob", "text": "after", "ts": now}).encode()
        path.write_bytes(before + b"\n" + b"x" * (record_cap * 8) + b"\n" + after + b"\n")

        history = ChannelHistory(observe_max_entries=10, history_dir=tmp_path)
        real_open = history._open_observe_file
        peak_read = 0

        class _ReadProbe:
            def __init__(self, inner):
                self._inner = inner

            def _record(self, chunk):
                nonlocal peak_read
                peak_read = max(peak_read, len(chunk))
                return chunk

            def read(self, size=-1):
                return self._record(self._inner.read(size))

            def readline(self, size=-1):
                return self._record(self._inner.readline(size))

            def __iter__(self):
                return self

            def __next__(self):
                return self._record(next(self._inner))

            def __enter__(self):
                self._inner.__enter__()
                return self

            def __exit__(self, *exc):
                return self._inner.__exit__(*exc)

            def __getattr__(self, name):
                return getattr(self._inner, name)

        def _open_with_probe(leaf, root_identity):
            return _ReadProbe(real_open(leaf, root_identity))

        with monkeypatch.context() as patch:
            patch.setattr(history, "_open_observe_file", _open_with_probe)
            with caplog.at_level(logging.WARNING, logger=channel_history.__name__):
                history.set_observe("C1")
                _drain_lane()

        assert [entry.text for entry in history._channels["C1"]] == ["before", "after"]
        assert history._live_records["C1"] == 3
        assert (
            peak_read <= record_cap + 2
        ), f"history load materialized a {peak_read}-byte read; record cap is {record_cap}"
        oversized_warnings = [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.WARNING
            and "record over" in record.getMessage()
            and repr(path) in record.getMessage()
        ]
        assert len(oversized_warnings) == 1, oversized_warnings

    def test_maximum_sized_persisted_record_round_trips(self, tmp_path):
        """Every field at its writer bound remains loadable in the cache."""
        worst_id = "\x00" * channel_history.HISTORY_MAX_ID_CHARS
        worst_text = "\x00" * channel_history.HISTORY_MAX_TEXT_CHARS
        history = ChannelHistory(history_dir=tmp_path)
        history.set_observe("C1")
        history.push(
            "C1",
            worst_id,
            worst_text,
            thread_ts=worst_id,
            msg_ts=worst_id,
        )
        _drain_lane()

        reloaded = ChannelHistory(history_dir=tmp_path)
        reloaded.set_observe("C1")
        _drain_lane()

        assert len(reloaded._channels["C1"]) == 1
        entry = reloaded._channels["C1"][0]
        assert (entry.user, entry.text, entry.thread_ts, entry.msg_ts) == (
            worst_id,
            worst_text,
            worst_id,
            worst_id,
        )

    def test_a_busy_channel_rotates_without_a_rewrite(self, tmp_path):
        """More than a cap of appends yields a ``.1`` generation, never a rewrite.

        The live file holds at most the cap, the older generation is the
        previous live file byte for byte, and the union of both files holds
        every record from the newest full generation onward.
        """
        cap = 5
        h = ChannelHistory(observe_max_entries=cap, history_dir=tmp_path)
        h.set_observe("C1")
        for i in range(cap):
            h.push("C1", "alice", f"msg{i}")
        _drain_lane()
        live = tmp_path / "C1.jsonl"
        older = tmp_path / "C1.jsonl.1"
        full_generation = live.read_bytes()
        assert not older.exists(), "rotation fired before the live file reached the cap"

        for i in range(cap, 2 * cap - 1):
            h.push("C1", "alice", f"msg{i}")
        _drain_lane()

        assert older.exists(), "a busy channel never rotated its live file"
        assert older.read_bytes() == full_generation, "the older generation was re-serialized"
        assert len(self._lines(tmp_path)) <= cap
        assert self._texts(tmp_path) == [f"msg{i}" for i in range(cap, 2 * cap - 1)]
        assert self._union(tmp_path) == [f"msg{i}" for i in range(2 * cap - 1)]

    def test_an_inherited_oversized_single_file_is_rotated_aside_not_rewritten(self, tmp_path):
        """A file written by a build that never bounded it is moved to ``.1`` whole.

        After the load, memory holds at most the window; the oversized file
        sits untouched in the older generation, and the next rotation
        replaces it with a cap-sized one, bounding disk without losing any
        record newer than the retained window.
        """
        cap = 5
        path = tmp_path / "C1.jsonl"
        original = self._write_records(path, [f"msg{i}" for i in range(40)], ts=time.time() - 1)

        h = ChannelHistory(observe_max_entries=cap, history_dir=tmp_path)
        h.set_observe("C1")
        _drain_lane()

        assert [entry.text for entry in h._channels["C1"]] == [f"msg{i}" for i in range(35, 40)]
        assert not path.exists(), "the oversized file was left as the live generation"
        older = tmp_path / "C1.jsonl.1"
        assert older.read_text(encoding="utf-8") == original, "the inherited file was rewritten"

        # The next cap of appends fills a fresh live file, then one more rotates it
        # over the inherited generation: disk is bounded to two cap-sized files.
        for i in range(40, 40 + cap + 1):
            h.push("C1", "alice", f"msg{i}")
        _drain_lane()

        assert self._texts(tmp_path, generation=".1") == [f"msg{i}" for i in range(40, 45)]
        assert self._texts(tmp_path) == ["msg45"]
        assert len(self._lines(tmp_path)) + len(self._lines(tmp_path, generation=".1")) <= 2 * cap
        # Nothing newer than the retained window was lost across the load.
        assert [entry.text for entry in h._channels["C1"]] == [f"msg{i}" for i in range(41, 46)]
        reloaded = ChannelHistory(observe_max_entries=cap, history_dir=tmp_path)
        reloaded.set_observe("C1")
        assert [entry.text for entry in reloaded._channels["C1"]] == [
            f"msg{i}" for i in range(41, 46)
        ]

    def test_load_never_writes_a_record(self, tmp_path):
        """Whatever the file holds, a load only ever reads it, renames it whole or unlinks it."""
        cap = 3
        now = time.time()
        path = tmp_path / "C1.jsonl"
        older = tmp_path / "C1.jsonl.1"
        older_content = self._write_records(older, ["a0", "a1"], ts=now)
        live_content = "".join(
            [
                json.dumps({"user": "alice", "text": "b0", "msg_ts": "b0", "ts": now}) + "\n",
                "\n \t\n",
                "not json\n",
                json.dumps({"user": 7, "text": "bad shape", "ts": now}) + "\n",
                json.dumps({"user": "u", "text": "ts-less", "ts": None}) + "\n",
                json.dumps({"user": "alice", "text": "expired", "ts": now - 10**7}) + "\n",
                json.dumps({"user": "alice", "text": "b1", "msg_ts": "b1", "ts": now}) + "\n",
            ]
        )
        path.write_text(live_content, encoding="utf-8")

        h = ChannelHistory(observe_max_entries=cap, history_dir=tmp_path)
        h.set_observe("C1")
        _drain_lane()

        assert [entry.text for entry in h._channels["C1"]] == ["a1", "b0", "b1"]
        assert older.read_text(encoding="utf-8") == older_content
        assert path.read_text(encoding="utf-8") == live_content

    def test_rotation_counter_is_seeded_from_the_live_file_across_restarts(self, tmp_path):
        """The bound holds across a restart: the live file's own records count."""
        cap = 5
        seed = ChannelHistory(observe_max_entries=cap, history_dir=tmp_path)
        seed.set_observe("C1")
        for i in range(3):
            seed.push("C1", "alice", f"msg{i}")
        _drain_lane()

        h = ChannelHistory(observe_max_entries=cap, history_dir=tmp_path)
        h.set_observe("C1")
        for i in range(3, cap):
            h.push("C1", "alice", f"msg{i}")
        _drain_lane()
        assert not (tmp_path / "C1.jsonl.1").exists(), "rotated below the cap after a restart"
        assert self._texts(tmp_path) == [f"msg{i}" for i in range(cap)]

        h.push("C1", "alice", f"msg{cap}")
        _drain_lane()
        assert self._texts(tmp_path, generation=".1") == [f"msg{i}" for i in range(cap)]
        assert self._texts(tmp_path) == [f"msg{cap}"]

    def test_a_missing_history_directory_does_not_reset_a_full_live_file(self, tmp_path):
        """A missing history directory pauses appends until rotation succeeds."""
        cap = 5
        history_root = tmp_path / "history"
        h = ChannelHistory(observe_max_entries=cap, history_dir=history_root)
        h.set_observe("C1")
        for i in range(cap):
            h.push("C1", "alice", f"msg{i}")
        _drain_lane()
        assert len(self._lines(history_root)) == cap

        moved_root = tmp_path / "history-away"
        history_root.rename(moved_root)
        h.push("C1", "alice", "while-missing")
        _drain_lane()

        moved_root.rename(history_root)
        for i in range(3):
            h.push("C1", "alice", f"after-restore-{i}")
        _drain_lane()

        live_lines = len(self._lines(history_root))
        assert live_lines <= cap, f"live file grew to {live_lines} lines; cap is {cap}"

    def test_a_persistent_rotation_failure_never_grows_the_live_file_past_the_cap(
        self, tmp_path, caplog
    ):
        """While rotation keeps failing, disk appends pause instead of growing the file.

        A directory planted at the ``.1`` name makes every rename fail. The
        live file must stay at the cap however many messages arrive, the
        in-memory window must keep the newest entries, and once the
        obstruction is gone the next push rotates and appends resume.
        """
        cap = 4
        h = ChannelHistory(observe_max_entries=cap, history_dir=tmp_path)
        h.set_observe("C1")
        older = tmp_path / "C1.jsonl.1"
        older.mkdir()  # a rename over a directory fails on every platform

        for i in range(cap * 5):
            h.push("C1", "alice", f"msg{i}")
            _drain_lane()
            assert (
                len(self._lines(tmp_path)) <= cap
            ), f"live file grew to {len(self._lines(tmp_path))} lines while rotation failed"
        assert self._texts(tmp_path) == [f"msg{i}" for i in range(cap)]
        assert older.is_dir(), "the planted directory was replaced"
        assert [entry.text for entry in h._channels["C1"]] == [
            f"msg{i}" for i in range(cap * 4, cap * 5)
        ], "the in-memory window lost the newest entries"
        warnings = [
            record
            for record in caplog.records
            if record.levelno == logging.WARNING
            and record.getMessage().startswith("Failed to rotate history file")
        ]
        assert len(warnings) == 1, "the pause warned on every message instead of once"

        older.rmdir()
        h.push("C1", "alice", "resumed")
        _drain_lane()

        assert self._texts(tmp_path, generation=".1") == [f"msg{i}" for i in range(cap)]
        assert self._texts(tmp_path) == ["resumed"]

    @pytest.mark.skipif(not channel_history._SUPPORTS_DIR_FD, reason="dir_fd append path")
    def test_zero_byte_write_failures_do_not_replace_a_full_older_generation(
        self, tmp_path, monkeypatch
    ):
        """Zero-byte failures cannot inflate the counter and replace full history."""
        cap = 3
        older = tmp_path / "C1.jsonl.1"
        older_original = self._write_records(older, [f"old{i}" for i in range(cap)])
        h = ChannelHistory(observe_max_entries=cap, history_dir=tmp_path)
        h.set_observe("C1")
        _drain_lane()

        real_fdopen = os.fdopen

        class _ZeroByteFailure:
            def __init__(self, inner):
                self._inner = inner

            def write(self, payload: str) -> int:
                raise OSError(errno.ENOSPC, "disk full before any byte landed")

            def fileno(self) -> int:
                return self._inner.fileno()

            def __enter__(self):
                return self

            def __exit__(self, *exc) -> None:
                self._inner.__exit__(*exc)

        def _zero_byte_fdopen(fd, mode="r", *args, **kwargs):
            inner = real_fdopen(fd, mode, *args, **kwargs)
            if mode == "a" and kwargs.get("encoding") == "utf-8":
                return _ZeroByteFailure(inner)
            return inner

        with monkeypatch.context() as patch:
            patch.setattr(os, "fdopen", _zero_byte_fdopen)
            for i in range(cap * 2):
                h.push("C1", "alice", f"failed{i}")
                _drain_lane()

        h.push("C1", "alice", "recovered")
        _drain_lane()

        assert (
            older.read_text(encoding="utf-8") == older_original
        ), "zero-byte failures replaced the full older generation"
        assert self._texts(tmp_path) == ["recovered"]

    @pytest.mark.skipif(not channel_history._SUPPORTS_DIR_FD, reason="dir_fd append path")
    def test_unknown_live_count_blocks_rotation_until_remeasurement_succeeds(
        self, tmp_path, monkeypatch
    ):
        """An unreadable recount pauses disk writes until the live count is known."""
        cap = 3
        live = tmp_path / "C1.jsonl"
        older = tmp_path / "C1.jsonl.1"
        live_original = self._write_records(live, ["live0", "live1"])
        older_original = self._write_records(older, [f"old{i}" for i in range(cap)])
        h = ChannelHistory(observe_max_entries=cap, history_dir=tmp_path)
        h.set_observe("C1")
        _drain_lane()

        real_fdopen = os.fdopen
        real_open = h._open_observe_file
        measurement_fails = True

        class _ZeroByteFailure:
            def __init__(self, inner):
                self._inner = inner

            def write(self, payload: str) -> int:
                raise OSError(errno.ENOSPC, "disk full before any byte landed")

            def fileno(self) -> int:
                return self._inner.fileno()

            def __enter__(self):
                return self

            def __exit__(self, *exc) -> None:
                self._inner.__exit__(*exc)

        def _zero_byte_fdopen(fd, mode="r", *args, **kwargs):
            inner = real_fdopen(fd, mode, *args, **kwargs)
            if mode == "a" and kwargs.get("encoding") == "utf-8":
                return _ZeroByteFailure(inner)
            return inner

        def _fail_live_measurement(leaf, root_identity):
            if measurement_fails and leaf == live:
                raise OSError(errno.EIO, "live recount unavailable")
            return real_open(leaf, root_identity)

        with monkeypatch.context() as patch:
            patch.setattr(h, "_open_observe_file", _fail_live_measurement)
            with monkeypatch.context() as write_patch:
                write_patch.setattr(os, "fdopen", _zero_byte_fdopen)
                h.push("C1", "alice", "failed")
                _drain_lane()

            h.push("C1", "alice", "blocked")
            _drain_lane()
            assert (
                older.read_text(encoding="utf-8") == older_original
            ), "an unknown count replaced the older generation"
            assert (
                live.read_text(encoding="utf-8") == live_original
            ), "an append wrote while the live count was unknown"
            assert [entry.text for entry in h._channels["C1"]][-2:] == ["failed", "blocked"]

            measurement_fails = False
            h.push("C1", "alice", "recovered")
            _drain_lane()
            h.push("C1", "alice", "after-cap")
            _drain_lane()

        assert self._texts(tmp_path, generation=".1") == ["live0", "live1", "recovered"]
        assert self._texts(tmp_path) == ["after-cap"]
        assert [entry.text for entry in h._channels["C1"]] == [
            "blocked",
            "recovered",
            "after-cap",
        ]

    @pytest.mark.skipif(not channel_history._SUPPORTS_DIR_FD, reason="dir_fd append path")
    def test_repeated_partial_write_failures_never_grow_the_live_file_past_the_cap(
        self, tmp_path, monkeypatch
    ):
        """A write that fails after landing a partial line still counts toward the cap.

        Each failed write leaves one partial line behind; the lane counts the
        attempt, so the live file rotates on time and never holds more lines
        than the cap, and appends resume cleanly once writes succeed again.
        """
        cap = 3
        h = ChannelHistory(observe_max_entries=cap, history_dir=tmp_path)
        h.set_observe("C1")
        for i in range(cap - 1):
            h.push("C1", "alice", f"msg{i}")
        _drain_lane()
        live = tmp_path / "C1.jsonl"
        older = tmp_path / "C1.jsonl.1"

        def _line_count(path) -> int:
            return len(path.read_bytes().splitlines()) if path.exists() else 0

        real_fdopen = os.fdopen

        class _PartialWriter:
            """Lands a prefix of every payload, then fails like a full disk."""

            def __init__(self, inner):
                self._inner = inner

            def write(self, payload: str) -> int:
                self._inner.write(payload[:12])
                self._inner.flush()
                raise OSError(errno.ENOSPC, "disk full injected after a partial write")

            def fileno(self) -> int:
                return self._inner.fileno()

            def __enter__(self):
                return self

            def __exit__(self, *exc) -> None:
                self._inner.__exit__(*exc)

        def _partial_fdopen(fd, mode="r", *args, **kwargs):
            inner = real_fdopen(fd, mode, *args, **kwargs)
            if mode == "a" and kwargs.get("encoding") == "utf-8":
                return _PartialWriter(inner)
            return inner

        with monkeypatch.context() as patch:
            patch.setattr(os, "fdopen", _partial_fdopen)
            for i in range(cap - 1, cap * 4):
                h.push("C1", "alice", f"msg{i}")
                _drain_lane()
                assert (
                    _line_count(live) <= cap
                ), f"live file grew to {_line_count(live)} lines under failing writes"
        assert older.exists(), "the live file never rotated under failing writes"
        assert _line_count(older) <= cap

        h.push("C1", "alice", "recovered")
        _drain_lane()
        assert _line_count(live) <= cap
        reloaded = ChannelHistory(observe_max_entries=cap, history_dir=tmp_path)
        reloaded.set_observe("C1")
        assert [entry.text for entry in reloaded._channels["C1"]] == ["recovered"]

    @pytest.mark.skipif(not channel_history._SUPPORTS_DIR_FD, reason="dir_fd append path")
    def test_a_write_that_lands_then_fails_at_close_never_grows_the_live_file_past_the_cap(
        self, tmp_path, monkeypatch, caplog
    ):
        """A close failure recounts a fully landed, terminated payload exactly.

        The guarded recount finds the completed line and a clean file end, so
        later appends use the measured count without adding torn-line padding.
        A failure before any write moves neither the counter nor the torn flag.
        """
        cap = 3
        h = ChannelHistory(observe_max_entries=cap, history_dir=tmp_path)
        h.set_observe("C1")
        h.push("C1", "alice", "msg0")
        _drain_lane()
        live = tmp_path / "C1.jsonl"
        older = tmp_path / "C1.jsonl.1"

        def _line_count(path) -> int:
            return len(path.read_bytes().splitlines()) if path.exists() else 0

        real_fdopen = os.fdopen

        class _FailsAtClose:
            """Lands the whole payload, then fails the flush at close."""

            def __init__(self, inner):
                self._inner = inner

            def write(self, payload: str) -> int:
                written = self._inner.write(payload)
                self._inner.flush()
                return written

            def fileno(self) -> int:
                return self._inner.fileno()

            def __enter__(self):
                return self

            def __exit__(self, *exc) -> None:
                self._inner.__exit__(*exc)
                if exc[0] is None:
                    raise OSError(errno.EIO, "close failed after the write landed")

        def _fdopen_failing_at_close(fd, mode="r", *args, **kwargs):
            inner = real_fdopen(fd, mode, *args, **kwargs)
            if mode == "a" and kwargs.get("encoding") == "utf-8":
                return _FailsAtClose(inner)
            return inner

        with monkeypatch.context() as patch:
            patch.setattr(os, "fdopen", _fdopen_failing_at_close)
            h.push("C1", "alice", "msg1")  # lands, then the close fails
            _drain_lane()
        assert _line_count(live) == 2
        assert "C1" not in h._live_torn

        # A refusal before any write: the pin fails, nothing lands.
        records_before = h._live_records["C1"]

        def _refuse_pin(path, root_identity):
            raise OSError(errno.EACCES, "pin refused by test")

        with monkeypatch.context() as patch:
            patch.setattr(h, "_pin_history_parent", _refuse_pin)
            h.push("C1", "alice", "refused")
            _drain_lane()
        assert h._live_records["C1"] == records_before, "a pre-write failure moved the counter"
        assert "C1" not in h._live_torn, "a pre-write failure changed the torn flag"
        assert _line_count(live) == 2

        # Appends succeed again from the recounted line count and keep the
        # live file at or under the cap throughout.
        for i in range(2, 2 + cap * 3):
            h.push("C1", "alice", f"msg{i}")
            _drain_lane()
            assert (
                _line_count(live) <= cap
            ), f"live file grew to {_line_count(live)} lines after a failed close"
        assert older.exists(), "the live file never rotated after the failed close"
        assert _line_count(older) <= cap
        assert self._texts(tmp_path)[-1] == f"msg{1 + cap * 3}", "appends did not resume"
        assert [entry.text for entry in h._channels["C1"]][-1] == f"msg{1 + cap * 3}"

    def test_rotation_replaces_the_older_generation(self, tmp_path):
        """Exactly one previous generation is kept; each rotation replaces it."""
        cap = 2
        h = ChannelHistory(observe_max_entries=cap, history_dir=tmp_path)
        h.set_observe("C1")
        for i in range(7):
            h.push("C1", "alice", f"msg{i}")
        _drain_lane()

        assert self._texts(tmp_path, generation=".1") == ["msg4", "msg5"]
        assert self._texts(tmp_path) == ["msg6"]
        assert sorted(p.name for p in tmp_path.iterdir()) == ["C1.jsonl", "C1.jsonl.1"]

    def test_file_pair_stays_within_twice_the_cap_at_each_drain(self, tmp_path):
        """The two generations together never exceed twice the cap."""
        cap = 3
        h = ChannelHistory(observe_max_entries=cap, history_dir=tmp_path)
        h.set_observe("C1")

        for i in range(cap * 4):
            h.push("C1", "alice", f"msg{i}")
            _drain_lane()
            live = len(self._lines(tmp_path))
            older = len(self._lines(tmp_path, generation=".1"))
            assert live <= cap, f"live file grew to {live} lines; cap is {cap}"
            assert live + older <= 2 * cap, f"generations hold {live + older} lines"
            # Nothing from the newest full generation onward is ever missing.
            union = self._union(tmp_path)
            assert union == [f"msg{j}" for j in range(i + 1 - len(union), i + 1)]

    @pytest.mark.skipif(not platform_compat.IS_POSIX, reason="POSIX permission bits")
    def test_rotated_generations_keep_the_owner_only_mode(self, tmp_path):
        """A rename keeps the inode, so both generations stay owner-only."""
        h = ChannelHistory(observe_max_entries=2, history_dir=tmp_path)
        h.set_observe("C1")
        for i in range(3):
            h.push("C1", "alice", f"msg{i}")
        _drain_lane()

        assert (tmp_path / "C1.jsonl").stat().st_mode & 0o777 == 0o600
        assert (tmp_path / "C1.jsonl.1").stat().st_mode & 0o777 == 0o600

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
        the record.
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
        texts = self._texts(tmp_path)
        assert "appended" in texts, f"append skipped after a refused fchmod: {texts}"
        # The mode could not be tightened; the file keeps what the mount allowed.
        assert path.stat().st_mode & 0o777 == 0o644

    def test_appending_past_the_cap_does_not_grow_the_file_without_bound(self, tmp_path):
        """Rotation runs on the write path, not only at the next set_observe."""
        h = ChannelHistory(observe_max_entries=5, history_dir=tmp_path)
        h.set_observe("C1")
        for i in range(20):
            h.push("C1", "alice", f"msg{i}")
        _drain_lane()

        assert len(self._lines(tmp_path)) <= 5
        assert len(self._union(tmp_path)) <= 10
        assert self._union(tmp_path)[-5:] == [f"msg{i}" for i in range(15, 20)]

    def test_append_admission_semaphore_has_documented_64_slots(self, tmp_path):
        """The append queue's semaphore and declared cap stay at 64 slots."""
        h = ChannelHistory(history_dir=tmp_path)

        assert channel_history._DISK_LANE_MAX_PENDING == 64
        assert h._disk_lane_slots._value == channel_history._DISK_LANE_MAX_PENDING

    def test_the_reachable_window_is_intact_after_rotation(self, tmp_path):
        """Bounding the file must not cost the entries the deque still holds."""
        h = ChannelHistory(observe_max_entries=5, history_dir=tmp_path)
        h.set_observe("C1")
        for i in range(20):
            h.push("C1", "alice", f"msg{i}")

        ctx = h.context_for("C1")
        for i in range(15, 20):
            assert f"msg{i}" in ctx
        assert h.entry_count("C1") == 5

        # And it survives a restart that reloads from the two generations.
        _drain_lane()
        h2 = ChannelHistory(observe_max_entries=5, history_dir=tmp_path)
        h2.set_observe("C1")
        assert h2.entry_count("C1") == 5
        ctx2 = h2.context_for("C1")
        for i in range(15, 20):
            assert f"msg{i}" in ctx2

    def test_off_mode_context_merges_after_disk_entries_without_touching_disk(self, tmp_path):
        """Messages heard before observe was on follow the disk entries in memory."""
        cap = 4
        now = time.time()
        path = tmp_path / "C1.jsonl"
        original = self._write_records(path, [f"disk{i}" for i in range(cap)], ts=now)

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
        assert path.read_text(encoding="utf-8") == original

    def test_inherited_file_load_reads_into_a_cap_sized_deque(self, tmp_path, monkeypatch):
        """The lane retains only the newest cap of records: load memory is O(cap)."""
        cap = 3
        total = 20
        path = tmp_path / "C1.jsonl"
        self._write_records(path, [f"msg{index}" for index in range(total)])

        history = ChannelHistory(observe_max_entries=cap, history_dir=tmp_path)
        real_apply = history._apply_observe_load
        retained: list[list[str]] = []
        read_maxlens: list[int | None] = []

        def _recording_apply(channel_id, generation, entries):
            assert entries is not None
            retained.append([entry.text for entry in entries])
            read_maxlens.append(entries.maxlen)
            return real_apply(channel_id, generation, entries)

        monkeypatch.setattr(history, "_apply_observe_load", _recording_apply)
        history.set_observe("C1")
        _drain_lane()

        assert read_maxlens == [cap]
        assert retained == [[f"msg{index}" for index in range(total - cap, total)]]
        assert [entry.text for entry in history._channels["C1"]] == [
            f"msg{index}" for index in range(total - cap, total)
        ]

    def test_under_the_cap_nothing_is_rotated_or_dropped(self, tmp_path):
        """Negative control: the bound must not fire below the cap."""
        h = ChannelHistory(observe_max_entries=5, history_dir=tmp_path)
        h.set_observe("C1")
        for i in range(4):
            h.push("C1", "alice", f"msg{i}")
        _drain_lane()

        assert self._texts(tmp_path) == [f"msg{i}" for i in range(4)]
        assert not (tmp_path / "C1.jsonl.1").exists()

    def test_a_lowered_cap_takes_effect_at_the_next_rotation(self, tmp_path):
        """Lowering the cap rewrites nothing; the next appends rotate to the new bound."""
        old_cap = 5
        new_cap = 2
        h = ChannelHistory(observe_max_entries=old_cap, history_dir=tmp_path)
        h.set_observe("C1")
        for i in range(old_cap):
            h.push("C1", "alice", f"msg{i}")
        _drain_lane()
        path = tmp_path / "C1.jsonl"
        before = path.read_text(encoding="utf-8")

        h.set_observe_limits(new_cap, channel_history.OBSERVE_TTL_SECS)
        _drain_lane()
        assert path.read_text(encoding="utf-8") == before, "a quiet channel's file was touched"
        assert h._channels["C1"].maxlen == new_cap

        # The next append rotates the over-cap live file aside whole...
        h.push("C1", "alice", "msg5")
        _drain_lane()
        assert (tmp_path / "C1.jsonl.1").read_text(encoding="utf-8") == before
        assert self._texts(tmp_path) == ["msg5"]
        # ...and two more bring the pair within twice the new cap.
        h.push("C1", "alice", "msg6")
        h.push("C1", "alice", "msg7")
        _drain_lane()
        assert self._texts(tmp_path, generation=".1") == ["msg5", "msg6"]
        assert self._texts(tmp_path) == ["msg7"]
        assert len(self._union(tmp_path)) <= 2 * new_cap

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
        assert self._texts(tmp_path) == ["msg0", "msg1", "msg2", "msg3"]
        assert not (tmp_path / "C1.jsonl.1").exists(), "the raised cap did not lift the bound"

    def test_a_failed_load_never_blocks_appends_or_touches_existing_records(
        self, tmp_path, monkeypatch, caplog
    ):
        """A read failure costs the window, never the file or later appends.

        The unreadable live file's line count is unknown, so the lane treats
        it as full: the next append moves it aside whole (nothing in it is
        rewritten or lost) and persistence continues on a fresh live file.
        """
        path = tmp_path / "C1.jsonl"
        older = tmp_path / "C1.jsonl.1"
        original = self._write_records(path, ["persisted"])

        history = ChannelHistory(observe_max_entries=10, history_dir=tmp_path)
        real_open = history._open_observe_file

        def _fail_live(leaf, root_identity):
            if leaf == path:
                raise OSError("injected read failure")
            return real_open(leaf, root_identity)

        with monkeypatch.context() as patch:
            patch.setattr(history, "_open_observe_file", _fail_live)
            history.set_observe("C1")
        assert history.entry_count("C1") == 0
        assert any(
            record.getMessage().startswith("Refusing to read history file")
            for record in caplog.records
        )
        assert path.read_text(encoding="utf-8") == original, "the load touched an unreadable file"
        assert not older.exists()

        history.push("C1", "bob", "memory0")
        _drain_lane()

        assert older.read_text(encoding="utf-8") == original, "the moved-aside file was altered"
        assert self._texts(tmp_path) == ["memory0"]
        assert self._union(tmp_path) == ["persisted", "memory0"]

    def test_a_live_file_whose_read_fails_still_rotates_before_the_next_append(
        self, tmp_path, monkeypatch
    ):
        """A live file that exists but cannot be read is treated as full, not absent.

        Its line count is unknown to the lane, so the count is seeded at the
        cap: the first append after the load rotates the file aside before it
        writes, and the live file never holds more lines than the cap.
        """
        cap = 4
        path = tmp_path / "C1.jsonl"
        older = tmp_path / "C1.jsonl.1"
        original = self._write_records(path, [f"msg{i}" for i in range(cap)])

        h = ChannelHistory(observe_max_entries=cap, history_dir=tmp_path)
        real_open = h._open_observe_file
        failures = [PermissionError(errno.EACCES, "read refused once by test")]

        def _fail_live_once(leaf, root_identity):
            if leaf.name == "C1.jsonl" and failures:
                raise failures.pop()
            return real_open(leaf, root_identity)

        monkeypatch.setattr(h, "_open_observe_file", _fail_live_once)
        h.set_observe("C1")
        _drain_lane()
        assert h.entry_count("C1") == 0
        assert path.read_text(encoding="utf-8") == original, "the load touched the unreadable file"
        assert not older.exists(), "the load rotated or removed the unreadable file"

        def _line_count(leaf) -> int:
            return len(leaf.read_bytes().splitlines()) if leaf.exists() else 0

        for i in range(cap, cap * 4):
            h.push("C1", "alice", f"msg{i}")
            _drain_lane()
            assert (
                _line_count(path) <= cap
            ), f"live file grew to {_line_count(path)} lines after an unreadable load"
        assert older.exists(), "the first append after the failed read did not rotate"
        assert self._union(tmp_path)[-1] == f"msg{cap * 4 - 1}"

    def test_an_unreadable_live_file_is_never_rotated_over_a_readable_older_generation(
        self, tmp_path, monkeypatch, caplog
    ):
        """A readable ``.1`` beside an unreadable live file is the only loadable history.

        Renaming the unreadable file over it would destroy the one generation
        a later load can read, so the lane leaves both files exactly where
        they are and pauses the channel's disk appends until its history is
        reloaded. The in-memory window keeps every pushed message, and the
        pause lifts at the next load that can read the live file again.
        """
        cap = 4
        path = tmp_path / "C1.jsonl"
        older = tmp_path / "C1.jsonl.1"
        older_texts = [f"old{i}" for i in range(cap)]
        older_original = self._write_records(older, older_texts)
        live_original = self._write_records(path, [f"msg{i}" for i in range(cap)])

        h = ChannelHistory(observe_max_entries=cap, history_dir=tmp_path)
        real_open = h._open_observe_file
        failures = [PermissionError(errno.EACCES, "read refused once by test")]

        def _fail_live_once(leaf, root_identity):
            if leaf.name == "C1.jsonl" and failures:
                raise failures.pop()
            return real_open(leaf, root_identity)

        monkeypatch.setattr(h, "_open_observe_file", _fail_live_once)
        h.set_observe("C1")
        _drain_lane()
        assert [entry.text for entry in h._channels["C1"]] == older_texts
        assert older.read_text(encoding="utf-8") == older_original
        assert path.read_text(encoding="utf-8") == live_original, "the load touched the live file"

        pushed = [f"msg{i}" for i in range(cap, cap * 3)]
        for text in pushed:
            h.push("C1", "alice", text)
        _drain_lane()

        assert (
            older.read_text(encoding="utf-8") == older_original
        ), "the unreadable live file was rotated over the readable older generation"
        assert self._texts(tmp_path, generation=".1") == older_texts
        assert (
            path.read_text(encoding="utf-8") == live_original
        ), "the unreadable live file was renamed, removed or appended to"
        assert sorted(p.name for p in tmp_path.iterdir()) == ["C1.jsonl", "C1.jsonl.1"]
        assert [entry.text for entry in h._channels["C1"]] == pushed[-cap:]
        paused = [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.WARNING
            and "paused until a scheduled re-read" in record.getMessage()
        ]
        assert len(paused) == 1, paused
        assert str(path) in paused[0] and "C1" in paused[0], paused[0]

        # The next load reads the live file (the injected failure is spent), so
        # the pause lifts: the full live file rotates over ``.1`` as usual and
        # the append lands on a fresh live file.
        h.set_observe("C1")
        _drain_lane()
        h.push("C1", "alice", "after-reload")
        _drain_lane()
        assert older.read_text(encoding="utf-8") == live_original
        assert self._texts(tmp_path) == ["after-reload"]

    def test_oversized_live_file_is_not_rotated_over_an_unreadable_older_generation(
        self, tmp_path, monkeypatch, caplog
    ):
        """An unreadable older generation pauses writes beside an oversized live file."""
        cap = 4
        path = tmp_path / "C1.jsonl"
        older = tmp_path / "C1.jsonl.1"
        self._write_records(older, ["older"])
        older_original = older.read_bytes()
        self._write_records(path, [f"live{i}" for i in range(cap + 2)])
        live_original = path.read_bytes()

        h = ChannelHistory(observe_max_entries=cap, history_dir=tmp_path)
        real_open = h._open_observe_file

        def _fail_older(leaf, root_identity):
            if leaf == older:
                raise PermissionError(errno.EACCES, "read refused by test")
            return real_open(leaf, root_identity)

        with monkeypatch.context() as patch:
            patch.setattr(h, "_open_observe_file", _fail_older)
            h.set_observe("C1")
        _drain_lane()

        for i in range(3):
            h.push("C1", "alice", f"pushed{i}")
        _drain_lane()

        assert older.read_bytes() == older_original, "load replaced the unreadable older generation"
        assert path.read_bytes() == live_original, "load or appends changed the oversized live file"
        paused = [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.WARNING
            and "paused until a scheduled re-read" in record.getMessage()
        ]
        assert len(paused) == 1, paused
        assert str(path) in paused[0] and str(older) in paused[0], paused[0]

    def test_appends_never_rotate_a_filling_live_file_over_an_unreadable_older_generation(
        self, tmp_path, monkeypatch, caplog
    ):
        """An unreadable older generation pauses writes even beside an under-cap live file."""
        cap = 4
        path = tmp_path / "C1.jsonl"
        older = tmp_path / "C1.jsonl.1"
        self._write_records(older, ["older"])
        older_original = older.read_bytes()
        self._write_records(path, [f"live{i}" for i in range(cap - 2)])
        live_original = path.read_bytes()

        h = ChannelHistory(observe_max_entries=cap, history_dir=tmp_path)
        real_open = h._open_observe_file

        def _fail_older(leaf, root_identity):
            if leaf == older:
                raise PermissionError(errno.EACCES, "read refused by test")
            return real_open(leaf, root_identity)

        with monkeypatch.context() as patch:
            patch.setattr(h, "_open_observe_file", _fail_older)
            h.set_observe("C1")
        _drain_lane()

        for i in range(cap * 2):
            h.push("C1", "alice", f"pushed{i}")
        _drain_lane()

        assert older.read_bytes() == older_original, "an append rotated over the unreadable .1"
        assert path.read_bytes() == live_original, "appends reached the live file while paused"
        paused = [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.WARNING
            and "paused until a scheduled re-read" in record.getMessage()
        ]
        assert len(paused) == 1, paused
        assert str(path) in paused[0] and str(older) in paused[0], paused[0]

    def test_a_paused_channel_resumes_disk_appends_once_a_scheduled_re_read_succeeds(
        self, tmp_path, monkeypatch, caplog
    ):
        """A pause lifts on its own once the retry interval passes and every generation reads.

        An unreadable ``.1`` pauses the channel's disk appends at the load.
        Appends before the retry interval return without touching disk even
        after the file is readable again; the first append past the interval
        re-reads both generations, seeds the live count from the live file
        exactly as a load would, and writes. ``.1`` is never renamed,
        removed or written, and one INFO line records the resume.
        """
        cap = 4
        path = tmp_path / "C1.jsonl"
        older = tmp_path / "C1.jsonl.1"
        self._write_records(older, ["older"])
        older_original = older.read_bytes()
        live_texts = [f"live{i}" for i in range(cap - 2)]
        self._write_records(path, live_texts)
        live_original = path.read_bytes()

        h = ChannelHistory(observe_max_entries=cap, history_dir=tmp_path)
        real_open = h._open_observe_file
        unreadable = {older}

        def _refuse_unreadable(leaf, root_identity):
            if leaf in unreadable:
                raise PermissionError(errno.EACCES, "read refused by test")
            return real_open(leaf, root_identity)

        clock = [1000.0]
        with (
            monkeypatch.context() as patch,
            caplog.at_level(logging.INFO, logger=channel_history.__name__),
        ):
            patch.setattr(h, "_open_observe_file", _refuse_unreadable)
            patch.setattr(channel_history, "_pause_clock", lambda: clock[0])
            h.set_observe("C1")
            _drain_lane()
            h.push("C1", "alice", "while-paused")
            _drain_lane()
            assert "C1" in h._disk_paused
            assert path.read_bytes() == live_original, "an append wrote while paused"

            # The older generation is readable again, but the interval has not
            # passed: the append returns at once and re-reads nothing.
            unreadable.clear()
            clock[0] += channel_history._PAUSE_RETRY_INITIAL_SECS - 1
            h.push("C1", "alice", "before-interval")
            _drain_lane()
            assert "C1" in h._disk_paused
            assert path.read_bytes() == live_original, "an append wrote before the interval"

            clock[0] += 1
            h.push("C1", "alice", "after-interval")
            _drain_lane()

        assert "C1" not in h._disk_paused and "C1" not in h._pause_retry
        assert older.read_bytes() == older_original, "the re-read touched the older generation"
        assert self._texts(tmp_path) == live_texts + ["after-interval"]
        assert h._live_records["C1"] == len(live_texts) + 1, "the live count was not reseeded"
        assert "C1" not in h._live_torn and "C1" not in h._live_unknown
        pushed = ["while-paused", "before-interval", "after-interval"]
        assert [entry.text for entry in h._channels["C1"]] == (live_texts + pushed)[-cap:]
        resumed = [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.INFO and "disk appends resumed" in record.getMessage()
        ]
        assert len(resumed) == 1 and "C1" in resumed[0], resumed

        # The reseeded count drives the rotation as a load's would: the live
        # file fills to the cap, then the next append rotates it over ``.1``.
        h.push("C1", "alice", "fills-the-cap")
        _drain_lane()
        assert self._texts(tmp_path) == live_texts + ["after-interval", "fills-the-cap"]
        assert older.read_bytes() == older_original
        h.push("C1", "alice", "rotates")
        _drain_lane()
        assert self._texts(tmp_path, generation=".1") == live_texts + [
            "after-interval",
            "fills-the-cap",
        ]
        assert self._texts(tmp_path) == ["rotates"]

    def test_a_paused_channel_backs_off_while_a_generation_stays_unreadable(
        self, tmp_path, monkeypatch, caplog
    ):
        """A retry that still finds ``.1`` unreadable keeps the pause and doubles the wait.

        The re-read is a guarded read only: neither file changes. Between
        retries an append re-reads nothing, the wait doubles after each
        failed retry up to ``_PAUSE_RETRY_MAX_SECS``, and no line above DEBUG
        is logged after the load's single warning.
        """
        cap = 4
        path = tmp_path / "C1.jsonl"
        older = tmp_path / "C1.jsonl.1"
        self._write_records(older, ["older"])
        older_original = older.read_bytes()
        self._write_records(path, [f"live{i}" for i in range(cap - 2)])
        live_original = path.read_bytes()

        h = ChannelHistory(observe_max_entries=cap, history_dir=tmp_path)
        real_open = h._open_observe_file
        opens: list[str] = []

        def _refuse_older(leaf, root_identity):
            opens.append(leaf.name)
            if leaf == older:
                raise PermissionError(errno.EACCES, "read refused by test")
            return real_open(leaf, root_identity)

        initial = channel_history._PAUSE_RETRY_INITIAL_SECS
        maximum = channel_history._PAUSE_RETRY_MAX_SECS
        clock = [1000.0]
        with (
            monkeypatch.context() as patch,
            caplog.at_level(logging.INFO, logger=channel_history.__name__),
        ):
            patch.setattr(h, "_open_observe_file", _refuse_older)
            patch.setattr(channel_history, "_pause_clock", lambda: clock[0])
            h.set_observe("C1")
            _drain_lane()
            assert "C1" in h._disk_paused
            assert h._pause_retry["C1"].interval == initial
            paused = [
                record.getMessage()
                for record in caplog.records
                if record.levelno == logging.WARNING
                and "paused until a scheduled re-read" in record.getMessage()
            ]
            assert len(paused) == 1 and str(older) in paused[0], paused
            caplog.clear()
            opens.clear()

            # The first retry fires at the initial interval, re-reads both
            # generations, and finds ``.1`` still unreadable.
            clock[0] += initial
            h.push("C1", "alice", "retry-1")
            _drain_lane()
            assert "C1" in h._disk_paused
            assert sorted(opens) == ["C1.jsonl", "C1.jsonl.1"], opens
            assert h._pause_retry["C1"].interval == initial * 2
            opens.clear()

            # The wait doubled: an append at the old interval re-reads nothing.
            clock[0] += initial
            h.push("C1", "alice", "too-early")
            _drain_lane()
            assert "C1" in h._disk_paused
            assert opens == [], "an append re-read the files before the backed-off interval"

            # Each later retry re-reads exactly once and doubles the wait,
            # until the wait reaches the cap and stays there.
            while h._pause_retry["C1"].interval < maximum:
                interval = h._pause_retry["C1"].interval
                clock[0] = h._pause_retry["C1"].next_at
                h.push("C1", "alice", "retry")
                _drain_lane()
                assert sorted(opens) == ["C1.jsonl", "C1.jsonl.1"], opens
                opens.clear()
                assert h._pause_retry["C1"].interval == min(interval * 2, maximum)
            clock[0] = h._pause_retry["C1"].next_at
            h.push("C1", "alice", "retry-at-cap")
            _drain_lane()
            assert h._pause_retry["C1"].interval == maximum, "the wait grew past the cap"

        assert "C1" in h._disk_paused
        assert older.read_bytes() == older_original, "a retry touched the unreadable .1"
        assert path.read_bytes() == live_original, "an append wrote while paused"
        assert sorted(p.name for p in tmp_path.iterdir()) == ["C1.jsonl", "C1.jsonl.1"]
        above_debug = [
            record.getMessage()
            for record in caplog.records
            if record.levelno > logging.DEBUG and record.name == channel_history.__name__
        ]
        assert above_debug == [], "a failed retry logged above DEBUG"

    def test_a_generation_holding_no_parsable_record_is_removed_for_that_reason(
        self, tmp_path, caplog
    ):
        """A generation whose every line is unparsable is removed, and the log says why.

        Nothing in it can reach the window, so it goes the same way as a
        generation whose every record is past the TTL — but its records were
        never measured against the TTL, and the log line must not claim they
        were.
        """
        older = tmp_path / "C1.jsonl.1"
        older.write_bytes(b'{"user":"torn","text":"partial"')
        path = tmp_path / "C1.jsonl"
        original = self._write_records(path, ["fresh"])

        h = ChannelHistory(history_dir=tmp_path)
        with caplog.at_level(logging.INFO, logger=channel_history.__name__):
            h.set_observe("C1")
            _drain_lane()

        assert not older.exists(), "the generation with no parsable record was kept"
        assert path.read_text(encoding="utf-8") == original
        assert [entry.text for entry in h._channels["C1"]] == ["fresh"]
        removed = [
            record.getMessage()
            for record in caplog.records
            if record.getMessage().startswith("Removed history file")
        ]
        assert removed == [f"Removed history file {older}: holds no parsable record"], removed
        assert not any("past the TTL" in record.getMessage() for record in caplog.records)

    @staticmethod
    def _load_log(caplog) -> str:
        loaded = [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.INFO and record.getMessage().startswith("Loaded ")
        ]
        assert len(loaded) == 1, loaded
        return loaded[0]

    def test_load_log_names_an_unreadable_live_file_instead_of_claiming_a_full_generation(
        self, tmp_path, monkeypatch, caplog
    ):
        """The cap seeded for an unreadable live file is a rotation seed, not a count.

        With no readable ``.1`` beside it, the lane seeds the live file's
        record count at the cap so the next append rotates it aside. The load
        log must not present that seed as records found on disk: nobody
        counted the file, and it may hold far fewer lines than the cap.
        """
        cap = 4
        path = tmp_path / "C1.jsonl"
        self._write_records(path, ["only"])

        h = ChannelHistory(observe_max_entries=cap, history_dir=tmp_path)
        real_open = h._open_observe_file

        def _fail_live(leaf, root_identity):
            if leaf == path:
                raise PermissionError(errno.EACCES, "read refused by test")
            return real_open(leaf, root_identity)

        with monkeypatch.context() as patch:
            patch.setattr(h, "_open_observe_file", _fail_live)
            with caplog.at_level(logging.INFO, logger=channel_history.__name__):
                h.set_observe("C1")
                _drain_lane()

        message = self._load_log(caplog)
        assert f"({cap} records" not in message, f"the log claims a cap of records: {message}"
        assert str(path) in message and "could not be read" in message, message
        assert message.startswith("Loaded 0 entries for channel C1 from disk (0 records counted; ")
        assert h._live_records["C1"] == cap, "the rotation seed must stay at the cap"

    def test_load_log_counts_only_the_readable_older_generation_beside_an_unreadable_live_file(
        self, tmp_path, monkeypatch, caplog
    ):
        """An unreadable live file beside a readable ``.1`` is named, not counted as empty."""
        cap = 4
        path = tmp_path / "C1.jsonl"
        older = tmp_path / "C1.jsonl.1"
        self._write_records(older, ["old0", "old1", "old2"])
        self._write_records(path, ["live0", "live1"])

        h = ChannelHistory(observe_max_entries=cap, history_dir=tmp_path)
        real_open = h._open_observe_file

        def _fail_live(leaf, root_identity):
            if leaf == path:
                raise PermissionError(errno.EACCES, "read refused by test")
            return real_open(leaf, root_identity)

        with monkeypatch.context() as patch:
            patch.setattr(h, "_open_observe_file", _fail_live)
            with caplog.at_level(logging.INFO, logger=channel_history.__name__):
                h.set_observe("C1")
                _drain_lane()

        message = self._load_log(caplog)
        assert str(path) in message and "could not be read" in message, message
        assert message.startswith("Loaded 3 entries for channel C1 from disk (3 records counted; ")
        assert "in the two generations" not in message, message

    def test_load_log_names_an_unreadable_older_generation_instead_of_counting_it_as_empty(
        self, tmp_path, monkeypatch, caplog
    ):
        """An unreadable ``.1`` has no measured line count, so the log names it."""
        cap = 4
        path = tmp_path / "C1.jsonl"
        older = tmp_path / "C1.jsonl.1"
        self._write_records(older, ["old0", "old1"])
        self._write_records(path, ["live0", "live1", "live2"])

        h = ChannelHistory(observe_max_entries=cap, history_dir=tmp_path)
        real_open = h._open_observe_file

        def _fail_older(leaf, root_identity):
            if leaf == older:
                raise PermissionError(errno.EACCES, "read refused by test")
            return real_open(leaf, root_identity)

        with monkeypatch.context() as patch:
            patch.setattr(h, "_open_observe_file", _fail_older)
            with caplog.at_level(logging.INFO, logger=channel_history.__name__):
                h.set_observe("C1")
                _drain_lane()

        message = self._load_log(caplog)
        assert str(older) in message and "could not be read" in message, message
        assert message.startswith("Loaded 3 entries for channel C1 from disk (3 records counted; ")
        assert "in the two generations" not in message, message

    def test_torn_ascii_json_tail_never_swallows_the_next_append(self, tmp_path):
        """A torn ASCII JSON tail stays on disk; the next append starts a fresh line."""
        path = tmp_path / "C1.jsonl"
        valid = {
            "user": "alice",
            "text": "persisted",
            "msg_ts": "1",
            "ts": time.time(),
        }
        torn_tail = b'{"user":"torn","text":"partial"'
        path.write_bytes(json.dumps(valid).encode("utf-8") + b"\n" + torn_tail)

        history = ChannelHistory(observe_max_entries=4, history_dir=tmp_path)
        history.set_observe("C1")
        assert [entry.text for entry in history._channels["C1"]] == ["persisted"]
        _drain_lane()
        assert path.read_bytes().endswith(torn_tail), "the load rewrote the torn tail"

        history.push("C1", "bob", "appended", msg_ts="2")
        _drain_lane()

        # The append writes in text mode, so Windows lands "\r\n": compare
        # line-ending neutral bytes.
        on_disk = path.read_bytes().replace(b"\r\n", b"\n")
        assert torn_tail + b"\n{" in on_disk
        reloaded = ChannelHistory(observe_max_entries=4, history_dir=tmp_path)
        reloaded.set_observe("C1")
        assert [entry.text for entry in reloaded._channels["C1"]] == ["persisted", "appended"]

    def test_torn_tail_at_cap_minus_one_rotates_after_one_complete_append(self, tmp_path):
        """A torn tail already counts as one live line for rotation."""
        cap = 4
        path = tmp_path / "C1.jsonl"
        older = tmp_path / "C1.jsonl.1"
        self._write_records(older, [f"old{i}" for i in range(cap)])
        older_original = older.read_bytes()
        complete_live = self._write_records(path, [f"live{i}" for i in range(cap - 2)])
        torn_tail = b'{"user":"torn","text":"partial"'
        path.write_bytes(complete_live.encode("utf-8") + torn_tail)

        history = ChannelHistory(observe_max_entries=cap, history_dir=tmp_path)
        history.set_observe("C1")
        _drain_lane()

        history.push("C1", "bob", "fills-cap")
        _drain_lane()

        assert older.read_bytes() == older_original, "torn live tail rotated one record early"
        live_at_cap = path.read_bytes()
        assert len(live_at_cap.splitlines()) == cap
        assert live_at_cap.endswith(b"\n"), "new record lacks its terminating newline"
        assert json.loads(live_at_cap.splitlines()[-1])["text"] == "fills-cap"

        history.push("C1", "bob", "rotates-after-cap")
        _drain_lane()

        assert older.read_bytes() == live_at_cap, "live file did not rotate at the real cap"
        assert self._texts(tmp_path) == ["rotates-after-cap"]

    @pytest.mark.parametrize(
        "content",
        [
            pytest.param(b"", id="empty"),
            pytest.param(b'{"a": 1}\n{"b": 2}\n', id="ends-with-newline"),
            pytest.param(b'{"a": 1}\n{"user":"torn","text":"par', id="trailing-partial-line"),
            pytest.param(
                b"x" * (3 * channel_history._MEASURE_CHUNK_BYTES + 7) + b"\n",
                id="line-longer-than-chunk",
            ),
            pytest.param(
                b'{"a": 1}\n' + b"y" * (2 * channel_history._MEASURE_CHUNK_BYTES + 1),
                id="partial-line-longer-than-chunk",
            ),
        ],
    )
    def test_measurement_counts_lines_and_torn_end_like_the_load(self, tmp_path, content):
        """The chunked measurement and the load's scan agree on lines and torn state."""
        path = tmp_path / "C1.jsonl"
        path.write_bytes(content)
        h = ChannelHistory(history_dir=tmp_path)
        _root, root_identity = h._prepare_history_root()

        measured = h._measure_generation(path, root_identity)
        scanned = h._read_generation(path, root_identity, "C1", collections.deque(), 0.0)

        assert measured is not None and scanned is not None
        assert measured.readable and scanned.readable
        assert (measured.lines, measured.torn) == (scanned.lines, scanned.torn)
        assert measured.lines == content.count(b"\n") + (
            1 if content and not content.endswith(b"\n") else 0
        )

    def test_torn_utf8_tail_loads_valid_prefix_and_keeps_later_appends_intact(self, tmp_path):
        """A torn multibyte tail is skipped and cannot corrupt the record after it."""
        path = tmp_path / "C1.jsonl"
        valid = {
            "user": "alice",
            "text": "persisted",
            "msg_ts": "1",
            "ts": time.time(),
        }
        torn_tail = b'{"user":"u","text":"\xf0\x9f'
        path.write_bytes(json.dumps(valid).encode("utf-8") + b"\n" + torn_tail)

        history = ChannelHistory(observe_max_entries=4, history_dir=tmp_path)
        history.set_observe("C1")
        assert [entry.text for entry in history._channels["C1"]] == ["persisted"]

        history.push("C1", "bob", "memory0")
        _drain_lane()

        reloaded = ChannelHistory(observe_max_entries=4, history_dir=tmp_path)
        reloaded.set_observe("C1")
        assert [entry.text for entry in reloaded._channels["C1"]] == ["persisted", "memory0"]

    def test_a_complete_last_line_without_newline_survives_the_next_append(self, tmp_path):
        """An unterminated but valid last record is kept apart from the next one."""
        path = tmp_path / "C1.jsonl"
        record = json.dumps({"user": "alice", "text": "unterminated", "ts": time.time()})
        path.write_bytes(record.encode("utf-8"))

        history = ChannelHistory(observe_max_entries=4, history_dir=tmp_path)
        history.set_observe("C1")
        history.push("C1", "bob", "next")
        _drain_lane()

        assert self._texts(tmp_path) == ["unterminated", "next"]

    def test_undecodable_mid_file_line_is_skipped_without_a_rewrite(self, tmp_path):
        """An undecodable line is skipped in memory and left on disk."""
        path = tmp_path / "C1.jsonl"
        now = time.time()
        before = {"user": "alice", "text": "before", "msg_ts": "1", "ts": now}
        after = {"user": "bob", "text": "after", "msg_ts": "2", "ts": now}
        undecodable = b'{"user":"bad","text":"\xff"}\n'
        content = (
            json.dumps(before).encode("utf-8")
            + b"\n"
            + undecodable
            + json.dumps(after).encode("utf-8")
            + b"\n"
        )
        path.write_bytes(content)

        history = ChannelHistory(history_dir=tmp_path)
        history.set_observe("C1")
        _drain_lane()

        assert [entry.text for entry in history._channels["C1"]] == ["before", "after"]
        assert path.read_bytes() == content

    def test_two_records_with_the_same_text_both_load(self, tmp_path):
        """Repeated wording is two messages, not one; nothing collapses records."""
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
        assert h.entry_count("C1") == 2


class TestObserveDiskLaneOffLoop:
    """Every disk operation runs on the history lane, never on the event loop.

    ``push`` and ``set_observe`` are called synchronously from the gateway's
    async handlers, so every append, rotation, load and unlink is scheduled
    while a loop is running; the lane worker performs the IO, in submission
    order.
    """

    @staticmethod
    def _read_lines(path) -> list[str]:
        """Read the file, riding out the Windows sharing window.

        A rotation lands via rename; on Windows an AV or indexer handle on the
        freshly renamed file makes an immediate open fail with
        ``PermissionError`` while nothing is wrong. Bounded retry, then the
        real error surfaces.
        """
        for _ in range(20):
            try:
                return path.read_text(encoding="utf-8").strip().splitlines()
            except PermissionError:
                time.sleep(0.1)
        return path.read_text(encoding="utf-8").strip().splitlines()

    @classmethod
    def _texts(cls, path) -> list[str]:
        if not path.exists():
            return []
        return [json.loads(line)["text"] for line in cls._read_lines(path)]

    @staticmethod
    def _write_records(path, texts, ts=None):
        ts = time.time() if ts is None else ts
        content = "".join(
            json.dumps({"user": "alice", "text": text, "msg_ts": text, "ts": ts}) + "\n"
            for text in texts
        )
        path.write_text(content, encoding="utf-8")
        return content

    @staticmethod
    async def _drain_disk_lane():
        """Wait for lane work and anything its loop callback queues afterwards."""
        from kiro_crew.executors import channel_history_executor

        sentinel = channel_history_executor().submit(lambda: None)
        await asyncio.to_thread(sentinel.result, 10)
        # A deferred load applies on this loop; yield once so that callback
        # runs, then drain a second generation of lane work.
        await asyncio.sleep(0)
        sentinel = channel_history_executor().submit(lambda: None)
        await asyncio.to_thread(sentinel.result, 10)

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
    async def test_a_cap_raised_while_a_load_is_queued_keeps_every_record_within_it(self, tmp_path):
        """A live cap increase during a queued load loses nothing, in memory or on disk.

        The lane bounds its read by the cap in force when the read begins,
        and no load ever rewrites the file, so every record within the new
        cap is both in the window and still on disk afterwards.
        """
        from kiro_crew.executors import channel_history_executor

        path = tmp_path / "C1.jsonl"
        original = self._write_records(path, [f"msg{i}" for i in range(8)])

        h = ChannelHistory(observe_max_entries=2, history_dir=tmp_path)
        gate = threading.Event()
        channel_history_executor().submit(gate.wait, 30)  # wedge the worker
        try:
            h.set_observe("C1")  # the load is queued behind the wedge
            h.set_observe_limits(10, channel_history.OBSERVE_TTL_SECS)
        finally:
            gate.set()
        await self._drain_disk_lane()

        assert [entry.text for entry in h._channels["C1"]] == [f"msg{i}" for i in range(8)]
        assert h._channels["C1"].maxlen == 10
        on_disk = self._texts(tmp_path / "C1.jsonl.1") + self._texts(path)
        assert on_disk == [f"msg{i}" for i in range(8)], "the load discarded persisted records"
        # 8 records are within the raised cap of 10: the file is left where it is.
        assert path.read_text(encoding="utf-8") == original

    @pytest.mark.asyncio
    async def test_a_cap_lowered_while_a_load_is_queued_rewrites_nothing(self, tmp_path):
        """A live cap decrease during a queued load trims the window, not the file."""
        from kiro_crew.executors import channel_history_executor

        path = tmp_path / "C1.jsonl"
        original = self._write_records(path, [f"msg{i}" for i in range(5)])

        h = ChannelHistory(observe_max_entries=5, history_dir=tmp_path)
        gate = threading.Event()
        channel_history_executor().submit(gate.wait, 30)
        try:
            h.set_observe("C1")
            h.set_observe_limits(2, channel_history.OBSERVE_TTL_SECS)
        finally:
            gate.set()
        await self._drain_disk_lane()

        assert [entry.text for entry in h._channels["C1"]] == ["msg3", "msg4"]
        # Over the lowered cap with no older generation: rotated aside whole.
        assert (tmp_path / "C1.jsonl.1").read_text(encoding="utf-8") == original
        assert not path.exists()

    @pytest.mark.asyncio
    async def test_a_ttl_raised_while_a_load_runs_keeps_the_generation_it_now_covers(
        self, tmp_path, monkeypatch
    ):
        """The expired-generation unlink judges by the TTL in force after the read."""
        path = tmp_path / "C1.jsonl"
        self._write_records(path, ["aged0", "aged1"], ts=time.time() - 120)

        h = ChannelHistory(observe_max_entries=5, observe_ttl_secs=60, history_dir=tmp_path)
        started = threading.Event()
        release = threading.Event()
        real_open = h._open_observe_file
        held = False

        def _hold_first_open(leaf, root_identity):
            nonlocal held
            opened = real_open(leaf, root_identity)
            if not held:
                held = True
                started.set()
                if not release.wait(10):
                    opened.close()
                    raise TimeoutError("test did not release the held load")
            return opened

        monkeypatch.setattr(h, "_open_observe_file", _hold_first_open)
        h.set_observe("C1")
        assert await asyncio.to_thread(started.wait, 10)
        h.set_observe_limits(5, 3600)  # the aged records are inside the new TTL
        release.set()
        await self._drain_disk_lane()

        assert path.exists(), "a TTL raised during the read did not protect the generation"
        assert self._texts(path) == ["aged0", "aged1"]

    @pytest.mark.asyncio
    async def test_a_load_result_landing_after_observe_off_is_dropped(self, tmp_path):
        """A stale deferred load must not repopulate a disabled channel.

        If observe is switched off before the deferred result is applied, the
        unlink queued behind the read removes the file and the load
        generation check drops the stale result.
        """
        path = tmp_path / "C1.jsonl"
        self._write_records(path, [f"msg{i}" for i in range(10)])
        h = ChannelHistory(observe_max_entries=5, history_dir=tmp_path)
        h.set_observe("C1")  # result still pending on the lane
        h.unset_observe("C1")  # same tick: queues the UNLINK behind the read
        await self._drain_disk_lane()

        assert not path.exists(), "observe-off left the file behind"
        assert not (tmp_path / "C1.jsonl.1").exists()
        assert h.entry_count("C1") == 0, "a stale load result repopulated a disabled channel"

    @pytest.mark.asyncio
    async def test_pushes_racing_a_deferred_load_land_after_it(self, tmp_path):
        """Appends queued behind a pending load follow the loaded records on disk."""
        from kiro_crew.executors import channel_history_executor

        path = tmp_path / "C1.jsonl"
        original = self._write_records(path, ["disk0", "disk1"])

        h = ChannelHistory(observe_max_entries=3, history_dir=tmp_path)
        gate = threading.Event()
        channel_history_executor().submit(gate.wait, 30)
        try:
            h.set_observe("C1")
            h.push("C1", "bob", "new0", msg_ts="new0")
            assert path.read_text(encoding="utf-8") == original
        finally:
            gate.set()
        await self._drain_disk_lane()

        assert [entry.text for entry in h._channels["C1"]] == ["disk0", "disk1", "new0"]
        assert self._texts(path) == ["disk0", "disk1", "new0"]

    @pytest.mark.asyncio
    async def test_push_schedules_no_history_root_io_on_event_loop(self, tmp_path, monkeypatch):
        """Append and rotation scheduling do no filesystem work on the loop."""
        h = ChannelHistory(observe_max_entries=1, history_dir=tmp_path)
        h.set_observe("C1")
        loop_thread = threading.get_ident()
        calls: list[tuple[str, int]] = []
        real_pin_directory = platform_compat.pin_directory
        real_fstat = os.fstat
        real_rename = os.rename

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

        def _rename(*args, **kwargs):
            thread = threading.get_ident()
            calls.append(("rename", thread))
            assert thread != loop_thread, "push rotated the history file on the event loop"
            return real_rename(*args, **kwargs)

        with monkeypatch.context() as filesystem_probe:
            filesystem_probe.setattr(platform_compat, "pin_directory", _pin_directory)
            filesystem_probe.setattr(os, "fstat", _fstat)
            filesystem_probe.setattr(os, "rename", _rename)

            h.push("C1", "alice", "fills the cap")
            h.push("C1", "alice", "triggers rotation and appends")
            await self._drain_disk_lane()

        seen = {name for name, _thread in calls}
        assert {"pin_directory", "fstat"} <= seen
        if channel_history._SUPPORTS_DIR_FD:
            assert "rename" in seen

    def test_history_root_snapshot_never_blocks_on_preparer(self, tmp_path):
        """A loop-thread snapshot never waits on a preparer's filesystem IO.

        ``_prepare_history_root`` holds ``_history_root_lock`` across
        resolve/mkdir/pin/fstat on the lane. Loop-thread callers copy the root
        through ``_history_root_snapshot``; while the preparer holds the lock
        they must get the pre-trust answer (``None``) at once, not wait on
        lane IO.
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

        h.push("C1", "alice", "lands once the preparer is done")
        _drain_lane()
        assert "lands once the preparer is done" in (tmp_path / "C1.jsonl").read_text(
            encoding="utf-8"
        )

    @pytest.mark.asyncio
    async def test_load_time_rotation_of_an_inherited_file_runs_off_the_loop_thread(
        self, tmp_path, monkeypatch
    ):
        """The one-shot rotation of an oversized inherited file is offloaded too."""
        path = tmp_path / "C1.jsonl"
        self._write_records(path, [f"msg{i}" for i in range(40)], ts=time.time() - 1)
        seen: list[int] = []
        real_rename = os.rename
        real_replace = os.replace

        def _recording_rename(*args, **kwargs):
            seen.append(threading.get_ident())
            return real_rename(*args, **kwargs)

        def _recording_replace(*args, **kwargs):
            seen.append(threading.get_ident())
            return real_replace(*args, **kwargs)

        monkeypatch.setattr(os, "rename", _recording_rename)
        monkeypatch.setattr(os, "replace", _recording_replace)
        h = ChannelHistory(observe_max_entries=5, history_dir=tmp_path)
        h.set_observe("C1")
        await self._drain_disk_lane()

        assert seen, "the inherited oversized file was not rotated on load"
        assert all(ident != threading.get_ident() for ident in seen)
        assert len(self._read_lines(tmp_path / "C1.jsonl.1")) == 40
        assert not path.exists()

    @pytest.mark.asyncio
    async def test_a_stalled_disk_bounds_the_queue_instead_of_growing_it(
        self, tmp_path, monkeypatch
    ):
        """With the worker wedged, the queue stays bounded and admitted appends land.

        A stalled disk must not let queued closures accumulate one per
        message: the loop thread's submit stays non-blocking, and an append
        past ``_DISK_LANE_MAX_PENDING`` is dropped while the deque keeps its
        entry. Once the lane drains, the admitted appends are on disk in
        order, with no duplicates and nothing else.
        """
        from kiro_crew import channel_history as ch_mod
        from kiro_crew.executors import channel_history_executor

        cap = 100
        h = ChannelHistory(observe_max_entries=cap, history_dir=tmp_path)
        h.set_observe("C1")
        await self._drain_disk_lane()

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
        real_executor.submit(gate.wait, 30)  # wedge the single worker
        total = cap * 3
        try:
            for i in range(total):
                h.push("C1", "alice", f"msg{i}")
            assert submitted, "the counting executor never saw a submission"
            assert (
                len(submitted) <= ch_mod._DISK_LANE_MAX_PENDING
            ), "a stalled lane accepted more queued jobs than its bound"
        finally:
            gate.set()
        await self._drain_disk_lane()

        assert h.entry_count("C1") == cap, "the deque must keep every entry"
        texts = self._texts(tmp_path / "C1.jsonl")
        assert texts == [f"msg{i}" for i in range(ch_mod._DISK_LANE_MAX_PENDING)]
        assert not (tmp_path / "C1.jsonl.1").exists()

    @pytest.mark.asyncio
    async def test_unset_observe_survives_a_saturated_lane(self, tmp_path, monkeypatch):
        """Disabling observe is never droppable, even under saturation.

        The unlink bypasses the append bound and runs after every queued
        append on the single-worker lane — so after the lane drains, both
        generations are gone.
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
        assert not (tmp_path / "C1.jsonl.1").exists()

    @pytest.mark.asyncio
    async def test_an_append_right_after_the_rotating_push_opens_the_new_generation(self, tmp_path):
        """The push that rotates and the one after it both land in the fresh live file."""
        h = ChannelHistory(observe_max_entries=5, history_dir=tmp_path)
        h.set_observe("C1")
        for i in range(7):
            h.push("C1", "alice", f"msg{i}")  # the 6th push rotates first, then appends

        await self._drain_disk_lane()

        assert self._texts(tmp_path / "C1.jsonl.1") == [f"msg{i}" for i in range(5)]
        assert self._texts(tmp_path / "C1.jsonl") == ["msg5", "msg6"]

    @pytest.mark.asyncio
    async def test_unset_observe_unlink_is_not_overwritten_by_a_queued_write(self, tmp_path):
        """Disabling observe mode must win against writes queued before it."""
        h = ChannelHistory(observe_max_entries=5, history_dir=tmp_path)
        h.set_observe("C1")
        for i in range(6):
            h.push("C1", "alice", f"msg{i}")  # queues appends and a rotation
        h.unset_observe("C1")  # unlink submitted after them

        await self._drain_disk_lane()

        assert not (
            tmp_path / "C1.jsonl"
        ).exists(), "a queued history write resurrected the file unset_observe removed"
        assert not (tmp_path / "C1.jsonl.1").exists()

    @pytest.mark.asyncio
    async def test_off_loop_unlink_joins_the_lane_instead_of_running_inline(self, tmp_path):
        """An unlink scheduled off the loop must not touch disk inline.

        Inline IO on the calling thread runs concurrently with the lane
        worker, so an inline unlink could finish before a queued append that
        then recreates the file. Every unlink executes on the single-worker
        lane, so completion order equals schedule order regardless of the
        calling thread. Here the worker is wedged; if unset_observe ran its
        unlink inline, the file would vanish while the lane is still blocked.
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
            await asyncio.to_thread(h.unset_observe, "C1")
            assert path.exists(), "an off-loop unlink ran inline, bypassing the lane"
        finally:
            gate.set()
        await self._drain_disk_lane()
        assert not path.exists(), "the queued unlink never executed after the lane drained"

    @pytest.mark.asyncio
    async def test_a_failed_rotation_pauses_disk_appends_and_warns_once(
        self, tmp_path, monkeypatch, caplog
    ):
        """A rotation that fails is logged once, the disk append is skipped, and rotation retries.

        The live file stays at the cap while the rename keeps failing — every
        append retries the rotation and pauses when it fails, warning only on
        the first failure of a run — and the first rename that succeeds
        resumes persistence on a fresh live file.
        """
        cap = 3
        h = ChannelHistory(observe_max_entries=cap, history_dir=tmp_path)
        h.set_observe("C1")
        for i in range(cap):
            h.push("C1", "alice", f"msg{i}")
        await self._drain_disk_lane()
        path = tmp_path / "C1.jsonl"
        older = tmp_path / "C1.jsonl.1"

        real_rename = os.rename
        real_replace = os.replace
        fail = True

        def _rename(*args, **kwargs):
            if fail:
                raise OSError(errno.EBUSY, "rename refused by test")
            return real_rename(*args, **kwargs)

        def _replace(*args, **kwargs):
            if fail:
                raise OSError(errno.EBUSY, "rename refused by test")
            return real_replace(*args, **kwargs)

        monkeypatch.setattr(os, "rename", _rename)
        monkeypatch.setattr(os, "replace", _replace)

        h.push("C1", "alice", "msg3")  # would rotate: the rename fails, the write is skipped
        h.push("C1", "alice", "msg4")  # retries the rotation: fails again, skipped again
        await self._drain_disk_lane()
        assert self._texts(path) == ["msg0", "msg1", "msg2"], "the live file grew past the cap"
        assert not older.exists()
        assert [entry.text for entry in h._channels["C1"]] == ["msg2", "msg3", "msg4"]

        def _warnings() -> list[logging.LogRecord]:
            return [
                record
                for record in caplog.records
                if record.levelno == logging.WARNING
                and record.getMessage().startswith("Failed to rotate history file")
            ]

        assert len(_warnings()) == 1, "a persistent rotation failure warned on every message"
        assert _warnings()[0].exc_info is not None and _warnings()[0].exc_info[0] is OSError
        assert "paused" in _warnings()[0].getMessage()

        fail = False
        h.push("C1", "alice", "msg5")  # the live file is full: rotates first, then appends
        await self._drain_disk_lane()
        assert self._texts(older) == ["msg0", "msg1", "msg2"]
        assert self._texts(path) == ["msg5"]

        fail = True
        for i in range(6, 6 + cap):
            h.push("C1", "alice", f"msg{i}")  # fills the fresh live file, then fails to rotate
        await self._drain_disk_lane()
        assert self._texts(path) == ["msg5", "msg6", "msg7"]
        assert len(_warnings()) == 2, "a fresh run of failures after a success was not logged"

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
        """Load, append, and unlink keep C1 operations away from linked C2.

        The load refuses the link, so the lane treats the live name as full:
        the first append moves the link aside to ``.1`` by rename (a rename
        moves the directory entry and never follows it) and writes to a fresh
        regular file, and observe-off removes both entries without touching
        the link's target.
        """
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
        moved = history_dir / "C1.jsonl.1"
        assert moved.is_symlink(), "the planted link was not moved aside by the rotation"
        assert os.readlink(moved) == c2_path.name, "the moved link was resolved or altered"
        assert c1_path.is_file() and not c1_path.is_symlink()
        assert [json.loads(line)["text"] for line in c1_path.read_text().splitlines()] == [
            "must stay with C1"
        ]
        # A later load refuses the moved link too and reads only the real file.
        fresh = ChannelHistory(observe_max_entries=100, history_dir=history_dir)
        fresh.set_observe("C1")
        assert [entry.text for entry in fresh._channels["C1"]] == ["must stay with C1"]
        assert "C2 history" not in fresh.context_for("C1")

        h.unset_observe("C1")
        _drain_lane()
        assert c2_path.read_bytes() == original, "the unlink followed C1's linked leaf into C2"
        assert not c1_path.exists() and not c1_path.is_symlink()
        assert not moved.exists() and not moved.is_symlink()

    @pytest.mark.skipif(not platform_compat.IS_POSIX, reason="POSIX symlink semantics")
    def test_symlink_at_the_older_generation_is_refused_and_left_in_place_not_followed(
        self, tmp_path, caplog
    ):
        """A link planted at ``.1`` is skipped on load and left in place; appends pause."""
        history_dir = tmp_path / "history"
        history_dir.mkdir()
        victim = tmp_path / "victim.jsonl"
        original = (
            json.dumps({"user": "mallory", "text": "victim history", "ts": time.time()}) + "\n"
        ).encode()
        victim.write_bytes(original)
        link = history_dir / "C1.jsonl.1"
        link.symlink_to(victim)

        h = ChannelHistory(observe_max_entries=2, history_dir=history_dir)
        h.set_observe("C1")
        assert h.entry_count("C1") == 0, "the load followed a link at the older generation"
        assert any(
            record.getMessage().startswith("Refusing to read history file")
            and record.getMessage().endswith("C1.jsonl.1")
            for record in caplog.records
        )

        for i in range(3):
            h.push("C1", "alice", f"msg{i}")
        _drain_lane()

        assert link.is_symlink(), "the refused link at the older generation was replaced"
        assert os.readlink(link) == str(victim), "the refused link was altered"
        assert victim.read_bytes() == original, "an append or rotation wrote through the link"
        assert not (history_dir / "C1.jsonl").exists(), "appends ran while the channel was paused"
        assert [entry.text for entry in h._channels["C1"]] == ["msg1", "msg2"]
        paused = [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.WARNING
            and "paused until a scheduled re-read" in record.getMessage()
        ]
        assert len(paused) == 1, paused

    @pytest.mark.skipif(
        not (platform_compat.IS_POSIX and hasattr(os, "link")), reason="POSIX hard-link semantics"
    )
    def test_append_refuses_a_hard_link_at_the_live_leaf_and_leaves_its_target_alone(
        self, tmp_path, caplog
    ):
        """A hard link planted at the live name gets neither a record nor a chmod.

        A hard link is another name for the outside file's inode, so no path
        guard and no no-follow open can tell it from a real history file;
        only the link count on the opened descriptor can. The append must
        refuse it before the write and before the owner-only mode tightening,
        keep the entry in memory, and move neither the lane's line count nor
        its torn state, because no byte landed.
        """
        history_dir = tmp_path / "history"
        history_dir.mkdir()
        h = ChannelHistory(observe_max_entries=100, history_dir=history_dir)
        h.set_observe("C1")
        _drain_lane()  # the load seeds the lane's count at 0 for an absent file
        victim = tmp_path / "victim.jsonl"
        original = (
            json.dumps({"user": "mallory", "text": "victim history", "ts": time.time()}) + "\n"
        ).encode()
        victim.write_bytes(original)
        os.chmod(victim, 0o644)
        planted = history_dir / "C1.jsonl"
        os.link(victim, planted)
        assert victim.stat().st_nlink == 2

        h.push("C1", "alice", "must not land in the victim")
        _drain_lane()

        assert victim.read_bytes() == original, "the append wrote through the hard link"
        assert stat.S_IMODE(victim.stat().st_mode) == 0o644, "the append chmod'ed the link's target"
        assert planted.stat().st_ino == victim.stat().st_ino, "the planted link was replaced"
        assert victim.stat().st_nlink == 2, "the planted link was removed"
        assert h.entry_count("C1") == 1, "the refused append must stay recoverable in memory"
        assert "must not land in the victim" in h.context_for("C1")
        assert h._live_records.get("C1") == 0, "a refused open must not move the line count"
        assert "C1" not in h._live_torn and "C1" not in h._live_unknown
        assert any(
            record.getMessage().startswith("Failed to append to history file")
            for record in caplog.records
        ), "the hard-link refusal was not logged"

    @pytest.mark.skipif(
        not (platform_compat.IS_POSIX and hasattr(os, "link")), reason="POSIX hard-link semantics"
    )
    def test_hard_link_at_the_older_generation_is_refused_and_left_in_place(self, tmp_path, caplog):
        """A hard link planted at ``.1`` is skipped on load and left in place; appends pause.

        The load must not read the outside file's records into the window,
        must not unlink or rename the entry (which would drop the link, the
        only thing a load may ever remove there), and must leave the outside
        file's bytes and mode exactly as they were.
        """
        history_dir = tmp_path / "history"
        history_dir.mkdir()
        victim = tmp_path / "victim.jsonl"
        original = (
            json.dumps({"user": "mallory", "text": "victim history", "ts": time.time()}) + "\n"
        ).encode()
        victim.write_bytes(original)
        os.chmod(victim, 0o644)
        link = history_dir / "C1.jsonl.1"
        os.link(victim, link)
        assert victim.stat().st_nlink == 2

        h = ChannelHistory(observe_max_entries=2, history_dir=history_dir)
        h.set_observe("C1")
        assert h.entry_count("C1") == 0, "the load read the outside file through the hard link"
        assert "victim history" not in h.context_for("C1")
        assert any(
            record.getMessage().startswith("Refusing to read history file")
            and record.getMessage().endswith("C1.jsonl.1")
            for record in caplog.records
        ), "the hard-linked older generation was not refused"

        for i in range(3):
            h.push("C1", "alice", f"msg{i}")
        _drain_lane()

        assert link.stat().st_ino == victim.stat().st_ino, "the refused link was replaced"
        assert victim.stat().st_nlink == 2, "the refused link was unlinked"
        assert victim.read_bytes() == original, "a load, append or rotation wrote the target"
        assert stat.S_IMODE(victim.stat().st_mode) == 0o644, "the load chmod'ed the link's target"
        assert not (history_dir / "C1.jsonl").exists(), "appends ran while the channel was paused"
        assert [entry.text for entry in h._channels["C1"]] == ["msg1", "msg2"]
        paused = [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.WARNING
            and "paused until a scheduled re-read" in record.getMessage()
        ]
        assert len(paused) == 1, paused

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFO semantics")
    @pytest.mark.parametrize("generation", ["", ".1"])
    def test_load_refuses_a_fifo_at_either_generation_without_blocking(
        self, tmp_path, caplog, generation
    ):
        """A FIFO planted at a history generation is refused at once, not read.

        A blocking ``open`` of a FIFO with no writer waits forever, wedging
        the thread that enables observe (gateway boot). The load must open
        non-blocking and refuse anything that is not a regular file.
        """
        history_dir = tmp_path / "history"
        history_dir.mkdir()
        fifo = history_dir / f"C1.jsonl{generation}"
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
    async def test_rotation_refuses_a_parent_directory_swapped_for_a_link(self, tmp_path, caplog):
        """A rotation must not rename through a linked history directory.

        Rotation runs deferred like the appends, so it holds the same parent
        pin. The history directory is replaced by a link to a victim
        directory after the rotating push is scheduled; the pin refuses it,
        the victim directory stays empty, and the entry stays in the deque.
        """
        from kiro_crew.executors import channel_history_executor

        victim_dir = tmp_path / "victim_dir"
        victim_dir.mkdir()
        history_dir = tmp_path / "history"
        history_dir.mkdir()

        h = ChannelHistory(observe_max_entries=2, history_dir=history_dir)
        h.set_observe("C1")
        h.push("C1", "alice", "msg0")
        h.push("C1", "alice", "msg1")
        await self._drain_disk_lane()

        gate = threading.Event()
        channel_history_executor().submit(gate.wait, 30)  # wedge the worker
        try:
            h.push("C1", "alice", "msg2")  # rotates first when it reaches the lane
            (history_dir / "C1.jsonl").unlink()
            history_dir.rmdir()
            platform_compat.symlink_or_junction(victim_dir, history_dir)
        finally:
            gate.set()
        await self._drain_disk_lane()

        assert not list(victim_dir.iterdir()), "rotation or append wrote through a linked parent"
        platform_compat.unlink_link_or_junction(history_dir)
        assert [entry.text for entry in h._channels["C1"]] == ["msg1", "msg2"]
        assert any(
            record.getMessage().startswith("Failed to rotate history file")
            for record in caplog.records
        ), "the refused rotation was not logged"

    @pytest.mark.asyncio
    async def test_rotation_refuses_an_ancestor_swapped_after_scheduling(self, tmp_path, caplog):
        """A rotation must pin the same history root the load trusted."""
        from kiro_crew.executors import channel_history_executor

        trusted_anchor = tmp_path / "trusted"
        history_dir = trusted_anchor / "history"
        history_dir.mkdir(parents=True)
        moved_anchor = tmp_path / "trusted-before-swap"
        victim_anchor = tmp_path / "victim"
        victim_history = victim_anchor / "history"
        victim_history.mkdir(parents=True)
        (victim_history / "C1.jsonl").write_text("victim\n", encoding="utf-8")

        h = ChannelHistory(observe_max_entries=2, history_dir=history_dir)
        h.set_observe("C1")
        h.push("C1", "alice", "msg0")
        h.push("C1", "alice", "msg1")
        await self._drain_disk_lane()

        gate = threading.Event()
        channel_history_executor().submit(gate.wait, 30)
        try:
            h.push("C1", "alice", "msg2")  # rotates first when it reaches the lane
            trusted_anchor.rename(moved_anchor)
            platform_compat.symlink_or_junction(victim_anchor, trusted_anchor)
        finally:
            gate.set()
        try:
            await self._drain_disk_lane()
            assert not (
                victim_history / "C1.jsonl.1"
            ).exists(), "the rotation followed a swapped ancestor"
            assert (victim_history / "C1.jsonl").read_text(encoding="utf-8") == "victim\n"
            refusals = [
                record
                for record in caplog.records
                if record.getMessage().startswith("Failed to rotate history file")
            ]
            assert refusals, "the refused rotation was not logged"
            assert all(
                record.exc_info is not None
                and "history directory identity changed" in str(record.exc_info[1])
                for record in refusals
            )
        finally:
            platform_compat.unlink_link_or_junction(trusted_anchor)
            moved_anchor.rename(trusted_anchor)

    @pytest.mark.asyncio
    async def test_observe_off_then_on_keeps_the_window_in_memory_and_starts_a_fresh_file(
        self, tmp_path
    ):
        """An observe off-then-on toggle removes both generations and duplicates nothing.

        ``unset_observe`` keeps the newest ``max_entries`` window in memory;
        the unlink runs on the lane before the re-enable's load, which then
        finds no file, so the retained window stays in memory only and the
        next push starts a fresh live file holding just that push.
        """
        from kiro_crew.executors import channel_history_executor

        h = ChannelHistory(max_entries=2, observe_max_entries=3, history_dir=tmp_path)
        h.set_observe("C1")
        for i in range(4):
            h.push("C1", "alice", f"msg{i}")
        await self._drain_disk_lane()
        assert (tmp_path / "C1.jsonl.1").exists()

        release = threading.Event()
        channel_history_executor().submit(release.wait)
        try:
            h.unset_observe("C1")
            assert [entry.text for entry in h._channels["C1"]] == ["msg2", "msg3"]
            h.set_observe("C1")
        finally:
            release.set()
        await self._drain_disk_lane()

        assert not (tmp_path / "C1.jsonl").exists(), "observe-off left the live file behind"
        assert not (tmp_path / "C1.jsonl.1").exists(), "observe-off left the older generation"
        assert [entry.text for entry in h._channels["C1"]] == ["msg2", "msg3"]

        h.push("C1", "alice", "msg4")
        await self._drain_disk_lane()
        assert self._texts(tmp_path / "C1.jsonl") == ["msg4"]
        assert [entry.text for entry in h._channels["C1"]] == ["msg2", "msg3", "msg4"]

    @pytest.mark.asyncio
    async def test_boot_time_set_observe_touches_no_in_bound_file(self, tmp_path):
        """Enabling observe at startup over an in-bound file does no disk write."""
        seed = ChannelHistory(observe_max_entries=100, history_dir=tmp_path)
        seed.set_observe("C1")
        seed.push("C1", "alice", "persisted before restart")
        await self._drain_disk_lane()
        path = tmp_path / "C1.jsonl"
        before = path.read_bytes()

        fresh = ChannelHistory(observe_max_entries=100, history_dir=tmp_path)
        fresh.set_observe("C1")  # the boot-path call
        await self._drain_disk_lane()
        assert path.read_bytes() == before
        assert not (tmp_path / "C1.jsonl.1").exists()
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
