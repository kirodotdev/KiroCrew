"""Tests for channel history buffer."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time

import pytest

from kiro_crew import channel_history, executors
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
    # A test that exercised the exit drain must not leave admission closed
    # for every later test in this process — same contract as a survivable
    # exec-failure path in production. The gate is a close counter, so
    # release every hold, not just one.
    with executors._channel_history_admission_lock:
        executors._channel_history_drain_holds = 0


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
        _drain_lane()

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

    def test_under_the_cap_nothing_is_rewritten_or_dropped(self, tmp_path):
        """Negative control: the bound must not fire below the cap."""
        h = ChannelHistory(observe_max_entries=5, history_dir=tmp_path)
        h.set_observe("C1")
        for i in range(4):
            h.push("C1", "alice", f"msg{i}")
        _drain_lane()

        lines = self._lines(tmp_path)
        assert [json.loads(ln)["text"] for ln in lines] == [f"msg{i}" for i in range(4)]


class TestObserveCompactionOffLoop:
    """Compaction's ``atomic_write`` must never run on the event-loop thread.

    ``push`` and ``set_observe`` are called synchronously from the gateway's
    async handlers, so every compaction trigger fires while a loop is running.
    The serialization stays on the calling thread (the only mutator of the
    deque); only the finished bytes may be written on a worker.
    """

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
        lines = (tmp_path / "C1.jsonl").read_text(encoding="utf-8").strip().splitlines()
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
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 5, "oversized inherited file was not bounded on load"

    @staticmethod
    async def _drain_disk_lane():
        """Wait until every history-file mutation submitted so far has executed."""
        from kiro_crew.executors import channel_history_executor

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

        monkeypatch.setattr(
            "kiro_crew.executors.channel_history_executor",
            lambda: _CountingExecutor(),
        )
        # Wedge the single worker so nothing drains while we flood.
        real_executor.submit(gate.wait, 30)
        total = cap * 3  # crosses the compaction trigger three times
        try:
            for i in range(total):
                h.push("C1", "alice", f"msg{i}")
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
            f"msg{i}" for i in range(6)
        ], "an append submitted after the compaction snapshot was erased by its rename"

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
    async def test_a_failed_compaction_does_not_strand_queued_appends(self, tmp_path, monkeypatch):
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
        """
        from kiro_crew import channel_history as ch_mod
        from kiro_crew.executors import channel_history_executor

        def _failing_write(path, content):
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

    @pytest.mark.asyncio
    async def test_shutdown_drain_flushes_queued_history_writes(self, tmp_path):
        """The graceful-shutdown drain flushes the lane before os._exit.

        os._exit skips atexit, and the atexit hook's shutdown(wait=False,
        cancel_futures=True) would discard queued jobs anyway — so the
        gateway's shutdown path calls drain_channel_history_lane() before
        exiting. Because the lane is one worker, the drain's sentinel
        completing proves every queued history write ran. Bounded: with the
        worker wedged the drain reports False instead of delaying the exit.
        """
        from kiro_crew.executors import channel_history_executor, drain_channel_history_lane

        h = ChannelHistory(observe_max_entries=100, history_dir=tmp_path)
        h.set_observe("C1")

        gate = threading.Event()
        channel_history_executor().submit(gate.wait, 30)  # wedge the worker
        try:
            for i in range(3):
                h.push("C1", "alice", f"msg{i}")  # queued behind the wedge
            assert (
                await asyncio.to_thread(drain_channel_history_lane, 0.2) is False
            ), "a wedged lane must time out instead of delaying the exit"
        finally:
            gate.set()
        assert (
            await asyncio.to_thread(drain_channel_history_lane, 10.0) is True
        ), "the lane did not drain once the worker was released"

        lines = (tmp_path / "C1.jsonl").read_text(encoding="utf-8").strip().splitlines()
        assert [json.loads(ln)["text"] for ln in lines] == [
            f"msg{i}" for i in range(3)
        ], "queued writes did not land before the drain returned"

    @pytest.mark.asyncio
    async def test_deferred_append_never_recreates_a_removed_directory(self, tmp_path):
        """A queued append must not resurrect a directory removed after scheduling.

        The parent directory is created at SCHEDULE time; the deferred worker
        only opens. If the directory is removed between scheduling and the
        worker's open (a torn-down test tmp_path, an unset channel), the open
        fails ENOENT and is logged — the directory must stay gone.
        """
        from kiro_crew.executors import channel_history_executor

        history_dir = tmp_path / "history"
        history_dir.mkdir()
        h = ChannelHistory(observe_max_entries=100, history_dir=history_dir)
        h.set_observe("C1")

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

    @pytest.mark.asyncio
    async def test_append_refuses_a_symlink_swapped_in_after_scheduling(self, tmp_path):
        """A symlink swapped in at the history path must not be followed.

        The append runs deferred on the disk lane, and _observe_path's
        containment check resolves the path at SCHEDULE time — so a link
        created after scheduling but before the worker's open is invisible
        to it (TOCTOU). Two guards catch it across platforms: the
        ``O_NOFOLLOW`` open fails with ELOOP on the link where the flag
        exists, and ``platform_compat.open_append_no_reparse`` refuses to
        traverse a reparse point where it does not (Windows). Either way the
        linked target stays untouched and the entry stays recoverable in the
        deque.
        """
        try:
            (tmp_path / "_linkprobe").symlink_to(tmp_path / "victim.txt")
        except (OSError, NotImplementedError):
            pytest.skip("platform cannot create symlinks in this test env")

        from kiro_crew.executors import channel_history_executor

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
            (history_dir / "C1.jsonl").symlink_to(victim)
        finally:
            gate.set()
        await self._drain_disk_lane()

        assert (
            victim.read_text(encoding="utf-8") == "governance file contents\n"
        ), "the deferred append followed a symlink and wrote through it"
        assert h.entry_count("C1") >= 1, "the entry must survive in the deque"

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
        if not channel_history._SUPPORTS_DIR_FD:
            pytest.skip("platform cannot pin the parent directory (no dir_fd)")
        try:
            (tmp_path / "_linkprobe").symlink_to(tmp_path / "victim_dir")
        except (OSError, NotImplementedError):
            pytest.skip("platform cannot create symlinks in this test env")

        from kiro_crew.executors import channel_history_executor

        victim_dir = tmp_path / "victim_dir"
        victim_dir.mkdir()
        history_dir = tmp_path / "history"
        history_dir.mkdir()

        h = ChannelHistory(observe_max_entries=100, history_dir=history_dir)
        h.set_observe("C1")

        gate = threading.Event()
        channel_history_executor().submit(gate.wait, 30)  # wedge the worker
        try:
            # Scheduled while the parent is a real directory.
            h.push("C1", "alice", "attacker-controlled message")
            # Swap the PARENT for a link before the deferred open runs.
            (history_dir / "C1.jsonl").unlink(missing_ok=True)
            history_dir.rmdir()
            history_dir.symlink_to(victim_dir, target_is_directory=True)
        finally:
            gate.set()
        await self._drain_disk_lane()

        assert not (
            victim_dir / "C1.jsonl"
        ).exists(), "the deferred append wrote through a linked parent directory"
        assert h.entry_count("C1") >= 1, "the entry must survive in the deque"

    @pytest.mark.asyncio
    async def test_terminal_rewrite_refuses_a_parent_directory_swapped_for_a_link(self, tmp_path):
        """A compaction rewrite must not follow a linked history directory.

        Terminal ops run deferred like the appends, so they hold the same
        parent pin. The history directory is replaced by a link to a victim
        directory after the rewrite is scheduled; the pin refuses it, the
        victim directory stays empty, and nothing is published.
        """
        if not channel_history._SUPPORTS_DIR_FD:
            pytest.skip("platform cannot pin the parent directory (no dir_fd)")
        try:
            (tmp_path / "_linkprobe").symlink_to(tmp_path / "victim_dir")
        except (OSError, NotImplementedError):
            pytest.skip("platform cannot create symlinks in this test env")

        from kiro_crew.executors import channel_history_executor

        victim_dir = tmp_path / "victim_dir"
        victim_dir.mkdir()
        history_dir = tmp_path / "history"
        history_dir.mkdir()

        h = ChannelHistory(observe_max_entries=100, history_dir=history_dir)
        h.set_observe("C1")

        gate = threading.Event()
        channel_history_executor().submit(gate.wait, 30)  # wedge the worker
        try:
            h.push("C1", "alice", "attacker-controlled message")
            h._compact("C1")  # terminal rewrite scheduled while the dir is real
            (history_dir / "C1.jsonl").unlink(missing_ok=True)
            history_dir.rmdir()
            history_dir.symlink_to(victim_dir, target_is_directory=True)
        finally:
            gate.set()
        await self._drain_disk_lane()

        assert not (
            victim_dir / "C1.jsonl"
        ).exists(), "the terminal rewrite wrote through a linked parent directory"

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
    async def test_no_append_can_queue_behind_the_drain_sentinel(self, tmp_path):
        """A message arriving during the exit drain must not queue behind it.

        The drain closes admission BEFORE submitting its sentinel: a write
        admitted after the sentinel would only be discarded by the exec that
        follows, or worse, land torn. The refused entry stays in the deque;
        nothing reaches the file after the drain returns.
        """
        from kiro_crew.executors import channel_history_executor, drain_channel_history_lane

        h = ChannelHistory(observe_max_entries=100, history_dir=tmp_path)
        h.set_observe("C1")
        h.push("C1", "alice", "before drain")
        await self._drain_disk_lane()

        gate = threading.Event()
        channel_history_executor().submit(gate.wait, 30)  # wedge the worker
        try:
            # The drain times out against the wedge, but its gate is closed.
            assert await asyncio.to_thread(drain_channel_history_lane, 0.2) is False
            h.push("C1", "alice", "during drain")  # must be refused, not queued
        finally:
            gate.set()
        await self._drain_disk_lane()

        lines = (tmp_path / "C1.jsonl").read_text(encoding="utf-8").strip().splitlines()
        texts = [json.loads(ln)["text"] for ln in lines]
        assert texts == ["before drain"], "an append queued behind the drain sentinel"
        assert h.entry_count("C1") == 2, "the refused entry must survive in the deque"

    @pytest.mark.asyncio
    async def test_reopening_the_lane_after_a_failed_exec_restores_persistence(self, tmp_path):
        """A survivable exec failure must not leave persistence off forever.

        The drain closes admission on the way out of the process. Every path
        that can survive a failed exec (the auto-update handler catches and
        keeps serving) calls ``reopen_channel_history_lane`` — after it,
        appends must reach disk again.
        """
        from kiro_crew.executors import drain_channel_history_lane, reopen_channel_history_lane

        h = ChannelHistory(observe_max_entries=100, history_dir=tmp_path)
        h.set_observe("C1")

        assert await asyncio.to_thread(drain_channel_history_lane, 5.0) is True
        h.push("C1", "alice", "while closed")  # refused: gate is closed
        reopen_channel_history_lane()  # the failed-exec survivor's contract
        h.push("C1", "alice", "after reopen")
        await self._drain_disk_lane()

        lines = (tmp_path / "C1.jsonl").read_text(encoding="utf-8").strip().splitlines()
        texts = [json.loads(ln)["text"] for ln in lines]
        assert texts == ["after reopen"], "persistence did not resume after the reopen"

    @pytest.mark.asyncio
    async def test_failed_reexec_reopen_cannot_undo_a_concurrent_hard_exit_drain(self, tmp_path):
        """One reopen releases one drain's hold, never another drain's close.

        A failing re-exec reopens admission on its way back to serving. A
        concurrent hard-exit path (gateway shutdown) drained separately and
        is about to ``os._exit`` — its close must survive the other caller's
        reopen, or an append admitted in that window queues behind the
        hard-exit sentinel and dies with the exit. The gate is a close
        counter: admission reopens only when every drain's hold is released.
        """
        from kiro_crew.executors import drain_channel_history_lane, reopen_channel_history_lane

        h = ChannelHistory(observe_max_entries=100, history_dir=tmp_path)
        h.set_observe("C1")

        # Two independent drains: the re-exec attempt's and the hard-exit's.
        assert await asyncio.to_thread(drain_channel_history_lane, 5.0) is True
        assert await asyncio.to_thread(drain_channel_history_lane, 5.0) is True

        # The failed re-exec releases ITS hold. The hard-exit's hold remains:
        # admission must still refuse.
        reopen_channel_history_lane()
        h.push("C1", "alice", "during hard-exit window")
        await self._drain_disk_lane()
        assert not (tmp_path / "C1.jsonl").exists() or "during hard-exit window" not in (
            tmp_path / "C1.jsonl"
        ).read_text(encoding="utf-8"), "a reopen undid a concurrent drain's close"

        # Releasing the second hold restores persistence.
        reopen_channel_history_lane()
        h.push("C1", "alice", "after both released")
        await self._drain_disk_lane()
        text = (tmp_path / "C1.jsonl").read_text(encoding="utf-8")
        assert "after both released" in text, "admission did not reopen at zero holds"

    @pytest.mark.asyncio
    async def test_reenabling_observe_supersedes_a_queued_unlink(self, tmp_path):
        """An observe off-then-on toggle must not lose the reloaded history.

        ``unset_observe`` queues a coalesced UNLINK. ``set_observe`` reloads
        the still-present file synchronously; without a superseding terminal
        op the queued unlink later removes exactly the history that reload
        brought back. ``set_observe`` therefore publishes the reloaded
        window as a REWRITE — last-writer-wins over the pending unlink.
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

        # Wedge the lane so the toggle's UNLINK stays queued while
        # set_observe reloads the file synchronously.
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
    async def test_overlapping_reexecs_cannot_reopen_each_others_drain(self, monkeypatch):
        """A failed attempt's reopen must not clear a gate a second attempt closed.

        Two distinct restart paths can overlap on the one event loop. Without
        serialization, attempt A's exec failure reopens admission while
        attempt B's sentinel is already queued — an append admitted in that
        window queues behind B's sentinel and dies with B's exec.
        ``drain_and_reexec`` holds one loop-bound lock across its whole
        drain→exec→reopen section, so B's drain cannot start until A's
        failure-reopen has completed: the gate is CLOSED (not reopened by A)
        for the entire span between B's drain and B's exec.
        """
        from kiro_crew import executors as ex_mod

        order: list[str] = []

        def _failing_exec(module, argv, *, executable):
            order.append(f"exec:{executable}")
            raise OSError("exec failed (test)")

        monkeypatch.setattr("kiro_crew.platform_compat.reexec_python_module", _failing_exec)

        async def _attempt(name: str) -> None:
            try:
                await ex_mod.drain_and_reexec("kiro_crew", [], executable=name)
            except OSError:
                order.append(f"reopened-after:{name}")

        # Fire both attempts concurrently: the lock must serialize them whole.
        await asyncio.gather(_attempt("A"), _attempt("B"))

        assert order == [
            "exec:A",
            "reopened-after:A",
            "exec:B",
            "reopened-after:B",
        ], f"attempts interleaved: {order}"
        # And the gate ends OPEN: the last failed attempt reopened its own drain.
        assert ex_mod.submit_channel_history_job(lambda: None) is True

    def test_open_append_no_reparse_refuses_a_link_and_appends_to_a_file(self, tmp_path):
        """The no-follow append open refuses a link atomically.

        This is the single open the deferred append uses for the leaf on
        platforms without ``dir_fd``: a link at the name must fail the open
        itself (never resolve-then-check — on Windows resolving a UNC link
        fires outbound authentication), and a real file must append.
        """
        from kiro_crew import platform_compat

        real = tmp_path / "real.jsonl"
        fd = platform_compat.open_append_no_reparse(real)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write("one\n")
        fd = platform_compat.open_append_no_reparse(real)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write("two\n")
        assert real.read_text(encoding="utf-8") == "one\ntwo\n"

        victim = tmp_path / "victim.txt"
        victim.write_text("secret\n", encoding="utf-8")
        link = tmp_path / "link.jsonl"
        try:
            link.symlink_to(victim)
        except (OSError, NotImplementedError):
            pytest.skip("platform cannot create symlinks in this test env")
        with pytest.raises(OSError):
            platform_compat.open_append_no_reparse(link)
        assert victim.read_text(encoding="utf-8") == "secret\n"
