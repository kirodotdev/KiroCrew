"""Tests for the conversation log's storage layer.

Covers the bounded ``_LRUCache`` and the transcript and metadata caches built on
it, tail reads for recent-only access, thread safety under concurrent appends and
reads, the memoised recent window, the identity contract of a cached read, a
metadata read that survives a transient sharing violation, and the
``append_if_absent`` and delivery-id append paths.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from unittest.mock import MagicMock, patch

import pytest
from windows_sim import builtin_open_sharing_violation

from kiro_crew import history
from kiro_crew.history import _METADATA_CACHE_MAX, ConversationLog

# ---------------------------------------------------------------------------
# PR unit C — recommendation #3: bounded LRU transcript/metadata caches
# ---------------------------------------------------------------------------


class TestLRUCache:
    """Unit tests for the bounded _LRUCache primitive backing the caches."""

    def test_idle_entries_are_dropped_and_recent_ones_kept(self):
        from kiro_crew.history import _LRUCache

        c: _LRUCache[int] = _LRUCache(maxsize=8, idle_ttl=100.0)
        c["old"] = 1
        c["read-late"] = 2
        c["new"] = 3
        # flake-ok: pre-existing real-clock stamps set 500 s back against a 100 s idle TTL, moved verbatim; tracked for a de-flake pass
        now = time.monotonic()
        c._stamps["old"] = now - 500
        c._stamps["read-late"] = now - 500
        assert c.get("read-late") == 2  # a read refreshes the entry
        assert c.drop_idle() == 1
        assert "old" not in c
        assert c.get("read-late") == 2 and c.get("new") == 3

    def test_without_a_ttl_nothing_expires(self):
        from kiro_crew.history import _LRUCache

        c: _LRUCache[int] = _LRUCache(maxsize=8)
        c["a"] = 1
        # flake-ok: pre-existing real-clock base for an explicit sweep time, moved verbatim; tracked for a de-flake pass
        assert c.drop_idle(now=time.monotonic() + 10**6) == 0
        assert c.get("a") == 1

    def test_the_transcript_caches_expire_and_the_sweep_reaches_them(self, tmp_path):
        from kiro_crew import history_cache

        log = ConversationLog(tmp_path)
        log._msg_cache["k"] = ((0, 0, 0), 0, [{"role": "user", "content": "x"}])
        log._recent_cache["k\x00"] = ((0, 0, 0), 0, [])
        # flake-ok: pre-existing real-clock base for an explicit sweep time, moved verbatim; tracked for a de-flake pass
        later = time.monotonic() + history_cache._TRANSCRIPT_CACHE_IDLE_TTL + 1
        assert history_cache.sweep_idle_caches(now=later) >= 2
        assert "k" not in log._msg_cache
        assert "k\x00" not in log._recent_cache

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
        # Touch 'a' so it is not the LRU victim.
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
        # Bounds the metadata cache by assigning it directly rather than through a
        # constructor parameter: the production default deliberately does not follow
        # ``cache_max`` down, and a kwarg only tests pass is production API surface
        # with no product consumer. The eviction invariant under test is unchanged.
        from kiro_crew.history import _LRUCache

        log = ConversationLog(base_dir=tmp_path)
        log._meta_cache = _LRUCache(3)
        for i in range(10):
            key = f"sess{i}"
            log.append(key, "user", f"hi {i}")
            log.get_metadata(key)  # populate meta cache
        assert len(log._meta_cache) <= 3

    def test_a_small_cache_max_cannot_shrink_the_metadata_cache(self, tmp_path):
        """``cache_max`` must not drag the metadata cache down with it.

        ``list_sessions`` reads ``_meta_cache`` in one cyclic pass over the whole
        session directory, so a bound below the corpus size is evicted in exactly
        the order it will next be read and the hit rate collapses to ~0 — every
        call re-opens and re-parses the first line of most of the store. The
        transcript cache still honors the knob.
        """
        log = ConversationLog(base_dir=tmp_path, cache_max=8)
        assert log._msg_cache._maxsize == 8, "the transcript cache still honors cache_max"
        assert log._meta_cache._maxsize == _METADATA_CACHE_MAX, (
            "the metadata cache followed cache_max down; list_sessions will thrash "
            "on any store larger than that knob"
        )

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
        # so search still parses the whole file.
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

        # flake-ok: pre-existing 750 ms stress window before the threads are stopped, moved verbatim; tracked for a de-flake pass
        # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing 750 ms stress window before the threads are stopped, moved verbatim; tracked for a de-flake pass
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

        # flake-ok: pre-existing 500 ms stress window before the threads are stopped, moved verbatim; tracked for a de-flake pass
        # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing 500 ms stress window before the threads are stopped, moved verbatim; tracked for a de-flake pass
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

        # flake-ok: pre-existing 750 ms stress window before the threads are stopped, moved verbatim; tracked for a de-flake pass
        # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing 750 ms stress window before the threads are stopped, moved verbatim; tracked for a de-flake pass
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

        # flake-ok: pre-existing 500 ms stress window before the threads are stopped, moved verbatim; tracked for a de-flake pass
        # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing 500 ms stress window before the threads are stopped, moved verbatim; tracked for a de-flake pass
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


class TestMetadataReadSurvivesATransientSharingViolation:
    """A read that FAILED must not be reported as a session with no metadata.

    ``_read_metadata`` returning ``{}`` both for "this session has no metadata
    line" and for "I could not open the file" leaves callers unable to tell those
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
        assert (
            meta.get("agent") == "my-agent"
        ), f"a single transient sharing violation was reported as absence: {meta!r}"

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
            # flake-ok: pre-existing worker-wide patch of the stdlib time.sleep through history's _time, moved verbatim; tracked for a de-flake pass
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
        # flake-ok: pre-existing worker-wide patch of the stdlib time.sleep through history's _time, moved verbatim; tracked for a de-flake pass
        with patch.object(history_mod._time, "sleep", lambda s: slept.append(s)):
            with builtin_open_sharing_violation(match="s1.jsonl", times=1):
                meta = log.get_metadata("s1")

        assert slept, "no retry pause off the event loop"
        assert meta.get("agent") == "my-agent"

    def test_a_persistent_violation_still_reports_absence_but_warns(self, tmp_path, caplog):
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


class TestAppendIfAbsentOffLoop:
    """The returned future IS the contract: callers holding the only durable
    copy of something await it to turn "scheduled" into "on disk"."""

    @pytest.mark.asyncio
    async def test_returns_the_executor_future_when_a_loop_is_running(self) -> None:
        log = MagicMock()
        fut = history.append_if_absent_off_loop(log, "dashboard:s1", "assistant", "body")
        assert fut is not None, "the scheduled write was not handed back to the caller"
        # flake-ok: pre-existing unbounded await of the executor future, moved verbatim; tracked for a de-flake pass
        await fut
        log.append_if_absent.assert_called_once()

    @pytest.mark.asyncio
    async def test_the_returned_future_carries_the_append_failure(self) -> None:
        log = MagicMock()
        log.append_if_absent.side_effect = OSError("history lock contention")
        fut = history.append_if_absent_off_loop(log, "dashboard:s1", "assistant", "body")
        assert fut is not None
        with pytest.raises(OSError):
            # flake-ok: pre-existing unbounded await of the executor future, moved verbatim; tracked for a de-flake pass
            await fut

    def test_returns_none_when_written_inline(self) -> None:
        # No running loop: the append already happened, so there is nothing to
        # await and None is the correct answer, not a lost future.
        log = MagicMock()
        assert history.append_if_absent_off_loop(log, "dashboard:s1", "assistant", "body") is None
        log.append_if_absent.assert_called_once()


class TestAppendMid:
    """The append path can persist the window row's delivery identity.

    A durable injector writes one logical message twice — the window copy through
    ``_ChatSlot.append``, which mints ``meta.mid``, and the durable copy through
    this path. Passing that minted id here stores it in the SAME ``meta.mid``
    field shape the dashboard slot save writes, so the bounded-read identity walk
    (``_append_unflushed_tail``) recognises the durable copy as the window row's
    persisted form; a read cannot recover an identity the write never stored.
    """

    def test_append_persists_mid_in_the_save_paths_field_shape(self, tmp_path) -> None:
        log = ConversationLog(base_dir=tmp_path)
        log.append("t1", "assistant", "result", mid="m-feedfacefeedface")
        raw = (tmp_path / "t1.jsonl").read_text(encoding="utf-8").splitlines()
        row = json.loads(raw[1])
        assert row["meta"] == {"mid": "m-feedfacefeedface"}, (
            "the id must land exactly where the slot save writes it (meta.mid); "
            "any other spelling is invisible to the identity walk"
        )

    def test_id_less_legacy_append_still_round_trips_without_meta(self, tmp_path) -> None:
        # Pre-id transcripts hold rows with no ``meta`` at all. An append that
        # passes no id must keep producing that exact shape — readers keep an
        # id-less fallback for those rows, and nothing migrates old sessions.
        log = ConversationLog(base_dir=tmp_path)
        log.append("t1", "assistant", "legacy row")
        raw = (tmp_path / "t1.jsonl").read_text(encoding="utf-8").splitlines()
        row = json.loads(raw[1])
        assert "meta" not in row, "an id-less append must not grow a meta field"
        assert log.recent("t1", 5) == [{"role": "assistant", "content": "legacy row"}]

    def test_append_if_absent_writes_the_id_with_the_row(self, tmp_path) -> None:
        log = ConversationLog(base_dir=tmp_path)
        assert log.append_if_absent("t1", "assistant", "result", mid="m-0123456789abcdef") is True
        raw = (tmp_path / "t1.jsonl").read_text(encoding="utf-8").splitlines()
        row = json.loads(raw[1])
        assert row["meta"] == {"mid": "m-0123456789abcdef"}

    def test_append_if_absent_skips_only_its_own_persisted_copy(self, tmp_path) -> None:
        # Same body AND same id: this very message is already on disk (the slot
        # save or an earlier attempt of this write landed it) — skip, leaving
        # the persisted row byte-identical (an id is never retrofitted).
        log = ConversationLog(base_dir=tmp_path)
        log.append("t1", "assistant", "result", mid="m-0123456789abcdef")
        before = (tmp_path / "t1.jsonl").read_text(encoding="utf-8")
        assert log.append_if_absent("t1", "assistant", "result", mid="m-0123456789abcdef") is False
        assert (tmp_path / "t1.jsonl").read_text(encoding="utf-8") == before

    def test_append_if_absent_writes_a_new_occurrence_under_its_own_id(self, tmp_path) -> None:
        # Same body under ANOTHER id is a different occurrence that repeats the
        # text (an earlier injection's twin). Skipping on body alone would drop
        # this occurrence's only durable copy — the window is lost on restart —
        # so the write must land, carrying its own id.
        log = ConversationLog(base_dir=tmp_path)
        log.append("t1", "assistant", "result", mid="m-earlier0000000001")
        assert log.append_if_absent("t1", "assistant", "result", mid="m-newer000000000002") is True
        rows = [
            json.loads(line)
            for line in (tmp_path / "t1.jsonl").read_text(encoding="utf-8").splitlines()[1:]
        ]
        assert [r["meta"]["mid"] for r in rows] == [
            "m-earlier0000000001",
            "m-newer000000000002",
        ]

    def test_append_if_absent_treats_an_id_less_twin_as_another_occurrence(self, tmp_path) -> None:
        # A body-equal row with NO id is a pre-id legacy row; with an identity
        # in hand the caller cannot prove it is this message, and skipping
        # would silently lose the new occurrence. The legacy row itself stays
        # untouched (no migration).
        log = ConversationLog(base_dir=tmp_path)
        log.append("t1", "assistant", "result")
        assert log.append_if_absent("t1", "assistant", "result", mid="m-0123456789abcdef") is True
        rows = [
            json.loads(line)
            for line in (tmp_path / "t1.jsonl").read_text(encoding="utf-8").splitlines()[1:]
        ]
        assert "meta" not in rows[0], "the legacy row must not be migrated"
        assert rows[1]["meta"] == {"mid": "m-0123456789abcdef"}

    def test_append_if_absent_without_mid_keeps_body_only_matching(self, tmp_path) -> None:
        # An id-less caller keeps the body-equality regime: it holds no
        # identity, so body equality is all it can check — unchanged for every
        # existing caller that passes no mid.
        log = ConversationLog(base_dir=tmp_path)
        log.append("t1", "assistant", "result", mid="m-0123456789abcdef")
        before = (tmp_path / "t1.jsonl").read_text(encoding="utf-8")
        assert log.append_if_absent("t1", "assistant", "result") is False
        assert (tmp_path / "t1.jsonl").read_text(encoding="utf-8") == before

    def test_append_if_absent_off_loop_threads_the_id_through(self) -> None:
        # No running loop, so the wrapper takes the inline path; the contract
        # under test is only that *mid* survives the hop to the log method.
        # ``append_off_loop`` deliberately has no mid parameter: it has no
        # dual-write caller, and a parameter nothing consumes is surface.
        log = MagicMock()
        history.append_if_absent_off_loop(log, "k", "assistant", "body", mid="m-2")
        assert log.append_if_absent.call_args.kwargs["mid"] == "m-2"
