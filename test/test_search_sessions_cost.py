"""Cost regression tests for :meth:`ConversationLog.search_sessions`.

The palette dispatches one search per debounced keystroke, and every search
walks the whole recent corpus. The expensive half of that walk is folding each
session's content for case-insensitive matching (``str.casefold`` over the
corpus); the substring count itself is cheap. Folding also holds the GIL for its
full duration, so re-folding per query stalls the event loop even though the
handler runs the search in a worker thread.

These tests pin the invariant that makes the palette usable: the fold is paid
once per session per *change*, not once per query. They assert **fold counts**,
not wall-clock time — a timing threshold would measure the CI host's load rather
than this code, and a "warm is N times faster" ratio is exactly the flaky shape
the testing conventions rule out. A fold count is deterministic on every host.
"""

from __future__ import annotations

import os
import threading

import pytest

from kiro_crew import history
from kiro_crew.history import ConversationLog


@pytest.fixture
def counting_log(tmp_path, monkeypatch):
    """A log whose expensive fold is counted.

    Returns ``(log, calls)`` where ``calls`` is a list of the session keys that
    actually paid a fold, in order. Wrapping ``_build_folded`` (the cache-MISS
    half) rather than ``_folded_content`` is what makes the memoization
    observable: the latter is called once per session per query either way.
    """
    log = ConversationLog(base_dir=tmp_path)
    calls: list[str] = []
    original = ConversationLog._build_folded

    def counted(self, key: str, mtime: float):
        calls.append(key)
        return original(self, key, mtime)

    monkeypatch.setattr(ConversationLog, "_build_folded", counted)
    return log, calls


def _seed(log: ConversationLog, sessions: int = 6, messages: int = 4) -> None:
    for s in range(sessions):
        for m in range(messages):
            log.append(f"s{s}", "user", f"session {s} message {m} about deployment pipelines")


def _spy_on_snippets(monkeypatch) -> list[str]:
    """Record the session keys that paid a snippet build, in order."""
    seen: list[str] = []
    original = ConversationLog._content_snippet

    def counted(self, key: str, query: str):
        seen.append(key)
        return original(self, key, query)

    monkeypatch.setattr(ConversationLog, "_content_snippet", counted)
    return seen


class TestSearchFoldingIsMemoized:
    def test_repeated_query_folds_each_session_exactly_once(self, counting_log):
        log, calls = counting_log
        _seed(log)

        log.search_sessions("deployment", 50)
        assert sorted(calls) == [f"s{i}" for i in range(6)], "first query folds the whole corpus"

        calls.clear()
        log.search_sessions("deployment", 50)
        assert calls == [], "an identical repeat query must not re-fold anything"

    def test_typing_a_word_folds_the_corpus_once_not_once_per_keystroke(self, counting_log):
        """The palette's real access pattern: one query per debounced prefix.

        Each prefix is a DIFFERENT query string, so no query-level cache can
        help — only caching the folded corpus can. Without the fold cache this
        is 6 sessions x 5 prefixes = 30 folds.
        """
        log, calls = counting_log
        _seed(log)

        for prefix in ("de", "dep", "depl", "deplo", "deploy"):
            log.search_sessions(prefix, 50)

        assert len(calls) == 6, f"expected one fold per session, got {len(calls)}"

    def test_appending_to_one_session_refolds_only_that_session(self, counting_log):
        log, calls = counting_log
        _seed(log)
        log.search_sessions("deployment", 50)
        calls.clear()

        log.append("s3", "user", "one more note about deployment")
        log.search_sessions("deployment", 50)

        assert calls == ["s3"], "only the changed session may be re-folded"

    def test_out_of_band_content_change_is_picked_up(self, counting_log):
        """A write that bypasses ``append`` must still invalidate via mtime.

        The mtime guard is the backstop for edits this process did not make
        (another gateway generation, a manual edit). Without it the fold cache
        would serve stale content indefinitely.
        """
        log, calls = counting_log
        _seed(log, sessions=1)
        assert log.search_sessions("kangaroo", 50) == []
        calls.clear()

        path = log._path("s0")
        with open(path, "a", encoding="utf-8") as f:
            f.write('{"role": "user", "content": "kangaroo appeared"}\n')
        stat = path.stat()
        os.utime(path, (stat.st_atime, stat.st_mtime + 10))

        hits = log.search_sessions("kangaroo", 50)
        assert calls == ["s0"], "an mtime change must force a re-fold"
        assert [h["key"] for h in hits] == ["s0"]

    def test_compaction_invalidates_even_though_it_restores_the_mtime(self, counting_log):
        """``rewrite_session`` is the case the mtime guard CANNOT catch.

        Compaction deliberately restores the pre-write mtime so housekeeping
        does not reorder ``list_sessions``. Content changed but mtime did not,
        so only the explicit invalidation in ``_invalidate_cache`` keeps the fold
        cache honest — without it, search answers from the pre-compaction text
        forever.
        """
        log, _calls = counting_log
        log.append("s0", "user", "the platypus section")
        assert [h["key"] for h in log.search_sessions("platypus", 50)] == ["s0"]

        before = log._path("s0").stat().st_mtime
        log.rewrite_session("s0", [{"role": "user", "content": "the echidna section"}])
        assert log._path("s0").stat().st_mtime == before, "compaction must preserve mtime"

        assert log.search_sessions("platypus", 50) == [], "stale content must not still match"
        assert [h["key"] for h in log.search_sessions("echidna", 50)] == ["s0"]


class TestFoldCacheCoversTheScanWindow:
    """A bound smaller than the scan window would hit 0%, not degrade.

    ``search_sessions`` walks ``_SEARCH_SCAN_WINDOW`` sessions in the same order
    on every query. If the cache cannot hold them, each session is dropped one
    step before its next read, so the hit rate collapses to zero and the
    memoization silently stops working — precisely for the users with the most
    sessions, who need it most.

    The bound is BYTES rather than entries, because a session is read up to
    ``_SESSION_MAX_BYTES``: an entry count of 500 was anywhere from a few MB to
    ~1 GB depending on the corpus, so it bounded memory only by accident.
    ``_SearchTextCache`` preserves the no-collapse guarantee by refusing
    admission instead of evicting.
    """

    def test_fold_cache_is_bounded_by_bytes_not_entries(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        stats = log._folded_cache.stats()
        assert stats["max_bytes"] == history._SEARCH_FOLD_BUDGET_BYTES
        assert stats["max_bytes"] > 0, "the ceiling must actually be enforced"

    def test_a_small_cache_max_cannot_shrink_the_search_budgets(self, tmp_path):
        """The search memos hold derived text, not the parsed transcripts
        ``cache_max`` is tuned for, so they must not follow that knob down."""
        log = ConversationLog(base_dir=tmp_path, cache_max=8)
        assert log._folded_cache.stats()["max_bytes"] == history._SEARCH_FOLD_BUDGET_BYTES
        assert log._snippet_cache.stats()["max_bytes"] == history._SEARCH_SNIPPET_BUDGET_BYTES
        assert log._msg_cache._maxsize == 8, "the transcript cache still honors cache_max"

    def test_the_budget_holds_a_full_scan_window_of_ordinary_sessions(self, tmp_path):
        """The ceiling has to be generous enough to not be the common case.

        A budget tight enough to refuse ordinary corpora would reintroduce the
        very cliff this class exists to prevent, just measured in bytes. Pinned
        against a deliberately roomy per-session estimate so the assertion fails
        if someone tightens the budget without re-reasoning about the window.
        """
        generous_session_chars = 64 * 1024
        capacity = history._SEARCH_FOLD_BUDGET_BYTES // generous_session_chars
        assert capacity >= history._SEARCH_SCAN_WINDOW, (
            f"the fold budget holds only {capacity} sessions of "
            f"{generous_session_chars} chars, fewer than the "
            f"{history._SEARCH_SCAN_WINDOW}-session scan window"
        )

    def test_no_thrash_across_a_corpus_larger_than_the_transcript_cache(
        self, tmp_path, monkeypatch
    ):
        """The regression case: more sessions than ``_TRANSCRIPT_CACHE_MAX``.

        Sized at 256 while scanning 500, the second query would re-fold every
        single session. Kept small enough to stay a fast test but larger than the
        transcript cache it used to borrow its size from.
        """
        monkeypatch.setattr(history, "_TRANSCRIPT_CACHE_MAX", 8)
        monkeypatch.setattr(history, "_SEARCH_SCAN_WINDOW", 40)
        log = ConversationLog(base_dir=tmp_path, cache_max=8)
        for i in range(30):
            log.append(f"s{i:03d}", "user", f"session {i} mentions deployment")

        calls: list[str] = []
        original = ConversationLog._build_folded

        def counted(self, key: str, mtime: float):
            calls.append(key)
            return original(self, key, mtime)

        monkeypatch.setattr(ConversationLog, "_build_folded", counted)

        log.search_sessions("deployment", 5)
        assert len(calls) == 30, "first query folds every session"

        calls.clear()
        log.search_sessions("deployment", 5)
        assert calls == [], "a 30-session corpus must not thrash an 8-entry transcript cache"


class TestFoldDoesNotPinParsedTranscripts:
    """Folding must not warm ``_msg_cache`` for the whole scan window.

    ``_read_messages`` memoizes the parsed message dicts, which are an order of
    magnitude larger than the folded strings the search needs: routing the fold
    through it pinned ~330 MB of parsed dicts on a 136 MB corpus versus ~37 MB
    for the folds themselves. Searching must not cost that.
    """

    def test_scanning_does_not_populate_the_transcript_cache(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        for i in range(6):
            log.append(f"s{i}", "user", "mentions deployment")
        log._msg_cache.clear()

        log.search_sessions("nothingmatchesthis", 50)

        assert len(log._msg_cache) == 0, "a scan that returns nothing must parse nothing"

    def test_only_returned_rows_parse_their_transcript(self, tmp_path):
        """Search must never pin a parsed transcript at all.

        Both halves of a query now read the file directly — the fold, and the
        snippet for each returned row — so no part of searching warms
        ``_msg_cache``. That cache holds the parsed message dicts, which are an
        order of magnitude larger than the text search needs.
        """
        log = ConversationLog(base_dir=tmp_path)
        for i in range(10):
            log.append(f"s{i}", "user", "everyone mentions deployment")
        log._msg_cache.clear()

        hits = log.search_sessions("deployment", 2)

        assert len(hits) == 2
        assert all(h.get("snippet") for h in hits), "returned rows still carry snippets"
        assert len(log._msg_cache) == 0, "searching must parse no transcript"

    def test_the_fold_never_reads_the_parsed_cache(self, tmp_path):
        """``_msg_cache`` is filled by callers holding no write lock, so an entry
        can be a pre-rewrite parse stored under a restored mtime. Folding from it
        would launder that staleness into the search cache, which the fold's own
        lock cannot undo — so the fold reads the file and ignores that cache."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("s0", "user", "on disk only")
        log._read_messages("s0")  # warm the parsed cache
        assert len(log._msg_cache) == 1

        # Poison the parsed cache under the CURRENT mtime — an mtime guard alone
        # would happily reuse this entry.
        mtime = log._path("s0").stat().st_mtime
        log._msg_cache["s0"] = (mtime, [{"role": "user", "content": "phantom text"}])

        built = log._build_folded("s0", mtime)
        assert built is not None
        assert built[1] == "on disk only", "the fold must come from the file"
        assert "phantom" not in built[1]

    def test_the_fold_runs_while_the_keys_write_lock_is_held(self, tmp_path, monkeypatch):
        """The invariant that closes the preserved-mtime rewrite race.

        ``rewrite_session`` restores the pre-write mtime so compaction does not
        reorder ``list_sessions``. A fold that read before such a rewrite and
        stored after its ``_invalidate_cache`` would hold pre-rewrite text under a
        mtime the file still has — invisible to the mtime guard, so the newly
        saved messages would be missing from every later search for the life of
        the process.

        The fix is exclusion, not detection: the whole stat -> read -> store
        sequence runs inside the key's write lock. This asserts that directly
        (rather than racing real threads, which could only fail probabilistically)
        by tracking the lock's depth and sampling it from inside the fold.
        """
        depth = [0]
        real_file_lock = ConversationLog._file_lock

        class _TrackedLock:
            def __init__(self, inner):
                self._inner = inner

            def __enter__(self):
                self._inner.acquire()
                depth[0] += 1

            def __exit__(self, *exc):
                depth[0] -= 1
                self._inner.release()

        def tracked(self, key: str):
            return _TrackedLock(real_file_lock(self, key))

        real_build = ConversationLog._build_folded
        observed: list[int] = []

        def observing(self, key: str, mtime: float):
            observed.append(depth[0])
            return real_build(self, key, mtime)

        log = ConversationLog(base_dir=tmp_path)
        for i in range(3):
            log.append(f"s{i}", "user", "mentions deployment")

        monkeypatch.setattr(ConversationLog, "_file_lock", tracked)
        monkeypatch.setattr(ConversationLog, "_build_folded", observing)
        log.search_sessions("deployment", 50)

        assert observed, "the fold must actually have run"
        assert all(d >= 1 for d in observed), (
            f"every fold must run under the key's write lock, saw depths {observed}"
        )

    def test_a_writer_blocks_on_the_lock_the_fold_holds(self, tmp_path):
        """Pins the coupling that makes the exclusion real.

        ``_locked`` — which every append / rewrite / metadata edit goes through —
        acquires ``_file_lock(key)`` first, so holding that lock across the fold
        keeps writers out. If a future change gave writers a different lock, the
        fold's lock would still be taken and the test above would still pass while
        protecting nothing.
        """
        log = ConversationLog(base_dir=tmp_path)
        log.append("s0", "user", "first entry")

        writer_finished = threading.Event()

        def writer() -> None:
            log.append("s0", "user", "second entry")
            writer_finished.set()

        lock = log._file_lock("s0")
        lock.acquire()
        thread = threading.Thread(target=writer, daemon=True)
        thread.start()
        try:
            assert not writer_finished.wait(0.25), (
                "a writer must block while the fold's lock is held"
            )
        finally:
            lock.release()
        assert writer_finished.wait(10), "the writer must proceed once the lock is released"
        thread.join(timeout=10)

    def test_the_fold_lock_is_only_taken_on_a_miss(self, tmp_path, monkeypatch):
        """A warm search must not contend on any key's write lock.

        The lock is held across a file read plus a casefold, so taking it on the
        warm path would put every search in line behind chat appends.
        """
        log = ConversationLog(base_dir=tmp_path)
        for i in range(4):
            log.append(f"s{i}", "user", "mentions deployment")
        log.search_sessions("deployment", 50)  # warm every entry

        locked_for: list[str] = []
        original = ConversationLog._file_lock

        def counted(self, key: str):
            locked_for.append(key)
            return original(self, key)

        monkeypatch.setattr(ConversationLog, "_file_lock", counted)
        log.search_sessions("deployment", 50)

        assert locked_for == [], "a fully warm search must take no per-key lock"

    def test_a_read_failure_is_not_cached(self, tmp_path, monkeypatch):
        """A transient open failure must not make a session permanently unsearchable.

        The file's mtime does not change when a read fails, so caching the empty
        result would key it as current and the session would stay invisible until
        something wrote to it again.
        """
        log = ConversationLog(base_dir=tmp_path)
        log.append("s0", "user", "the bandicoot section")
        log._msg_cache.clear()

        real_open = open
        fail = {"on": True}

        def flaky_open(*args, **kwargs):
            if fail["on"] and str(args[0]) == str(log._path("s0")):
                raise OSError("transient")
            return real_open(*args, **kwargs)

        monkeypatch.setattr("builtins.open", flaky_open)
        assert log.search_sessions("bandicoot", 50) == []
        assert len(log._folded_cache) == 0, "a failed read must leave no cache entry"

        fail["on"] = False
        hits = log.search_sessions("bandicoot", 50)
        assert [h["key"] for h in hits] == ["s0"], "the next query must retry the read"

    def test_a_session_with_no_text_is_cached_as_empty(self, tmp_path):
        """``(0, "")`` from an EMPTY session is a real answer and stays cached —
        only a read FAILURE is uncacheable."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("s0", "user", "")
        log.search_sessions("anything", 50)
        assert len(log._folded_cache) == 1

    def test_unreadable_file_signals_failure_rather_than_empty(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        assert log._build_folded("never-existed", 0.0) is None

    def test_both_halves_of_a_query_share_one_definition_of_searchable_text(
        self, tmp_path, monkeypatch
    ):
        """The fold and the snippet must not drift apart on what they skip.

        They are two readers of the same on-disk format; divergent skip rules
        would let a query count a match the snippet cannot then locate (an empty
        snippet on a matched row), or vice versa.

        The guarantee is now structural rather than conventional: the fold hands
        the snippet memo the very list it folded, so a single traversal of
        ``_iter_message_texts`` serves both halves and there is no second
        traversal that could apply different rules. One call, not two — the
        second call is what this used to assert, and its disappearance is the
        optimization. The fallback path (memo refused or stale) still routes
        through the shared iterator; see ``TestSnippetSourceIsMemoized``.
        """
        log = ConversationLog(base_dir=tmp_path)
        log.append("s0", "user", "the wombat entry")

        callers: list[str] = []
        original = ConversationLog._iter_message_texts

        def counted(self, key: str):
            callers.append(key)
            return original(self, key)

        monkeypatch.setattr(ConversationLog, "_iter_message_texts", counted)
        hits = log.search_sessions("wombat", 50)

        assert [h["key"] for h in hits] == ["s0"]
        assert "wombat" in hits[0]["snippet"]
        assert callers == ["s0"], (
            "one traversal must serve both the fold and the snippet, "
            f"saw {callers}"
        )
        stored = log._snippet_cache.get("s0")
        assert stored is not None and stored[1] == ["the wombat entry"], (
            "the snippet memo must hold exactly what the fold read"
        )

    def test_the_shared_iterator_skips_metadata_and_unparseable_lines(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        log.append("s0", "user", "real content")
        with open(log._path("s0"), "a", encoding="utf-8") as handle:
            handle.write("\n")
            handle.write("not json at all\n")
            handle.write('["a list, not an object"]\n')
            handle.write('{"_type": "metadata", "content": "header text"}\n')
            handle.write('{"role": "user", "content": ""}\n')
            handle.write('{"role": "user", "content": 42}\n')
            handle.write('{"role": "user", "content": "second real"}\n')

        assert list(log._iter_message_texts("s0")) == ["real content", "second real"]

    def test_the_shared_iterator_stops_reading_when_the_caller_stops(self, tmp_path):
        """Early exit is what makes the snippet cheap — closing the generator must
        close the file rather than draining the whole transcript."""
        log = ConversationLog(base_dir=tmp_path)
        for i in range(200):
            log.append("s0", "user", f"message {i}")

        consumed = 0
        for _ in log._iter_message_texts("s0"):
            consumed += 1
            if consumed == 3:
                break
        assert consumed == 3


class TestSearchResultsUnchangedByMemoization:

    def test_case_insensitive_match_and_snippet_survive(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        log.append("alpha", "user", "The Straße was closed during the DEPLOYMENT window")
        log.append("beta", "user", "nothing relevant here")

        hits = log.search_sessions("strasse", 50)
        assert [h["key"] for h in hits] == ["alpha"], "casefold (ß -> ss) must still match"
        assert "Straße" in hits[0]["snippet"]

        hits = log.search_sessions("deployment", 50)
        assert [h["key"] for h in hits] == ["alpha"]
        assert "DEPLOYMENT" in hits[0]["snippet"]

    def test_match_does_not_span_two_messages(self, tmp_path):
        """The ``\\x00`` join must keep a match from bridging two messages."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("alpha", "user", "foo")
        log.append("alpha", "user", "bar")
        assert log.search_sessions("foobar", 50) == []

    def test_title_boost_still_outranks_a_body_mention(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        log.append("body", "user", "a passing mention of pipelines somewhere in here")
        log.append("titled", "user", "pipelines")
        log.update_metadata("titled", {"title": "pipelines rewrite"})

        hits = log.search_sessions("pipelines", 50)
        assert hits[0]["key"] == "titled"

    def test_snippet_is_only_built_for_matching_sessions(self, tmp_path, monkeypatch):
        """Non-matching sessions must not pay the snippet re-read.

        This is the other half of the per-query cost: the snippet needs the
        UNFOLDED text (offsets must line up), so it cannot come from the fold
        cache and has to be re-read. Building it for every scanned session would
        put a full re-read back on the hot path.
        """
        log = ConversationLog(base_dir=tmp_path)
        log.append("hit", "user", "contains the needle")
        for i in range(5):
            log.append(f"miss{i}", "user", "contains nothing of interest")

        snippet_for = _spy_on_snippets(monkeypatch)
        log.search_sessions("needle", 50)

        assert snippet_for == ["hit"]

    def test_snippet_cost_tracks_the_returned_rows_not_the_match_count(
        self, tmp_path, monkeypatch
    ):
        """20 sessions match, 3 are returned => 3 snippets, not 20.

        Snippets are attached after the sort+slice for exactly this reason: the
        palette shows a page of rows, so a broad query that matches the whole
        corpus must not cost a re-read per match.
        """
        log = ConversationLog(base_dir=tmp_path)
        for i in range(20):
            log.append(f"s{i}", "user", "everyone mentions deployment")

        snippet_for = _spy_on_snippets(monkeypatch)
        hits = log.search_sessions("deployment", 3)

        assert len(hits) == 3
        assert len(snippet_for) == 3, f"expected 3 snippet builds, got {len(snippet_for)}"

    def test_snippet_scan_stops_at_the_first_matching_message(self, tmp_path):
        """An early hit must not cost a fold of the rest of the session."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("s0", "user", "the needle is right here")
        for _ in range(50):
            log.append("s0", "user", "filler " * 200)

        hits = log.search_sessions("needle", 50)
        assert "needle" in hits[0]["snippet"]
        assert "filler" not in hits[0]["snippet"], "window must stay in the matching message"


class TestSearchTextCacheByteBudget:
    """The bound is bytes, and it is a ceiling rather than an eviction trigger.

    An entry count bounded nothing that mattered: a session is read up to
    ``_SESSION_MAX_BYTES``, so ``_SEARCH_SCAN_WINDOW`` entries ranged from a few
    MB to ~1 GB depending on whose corpus it was.
    """

    def _cache(self, max_bytes: int) -> history._SearchTextCache[str]:
        return history._SearchTextCache(max_bytes, len)

    def test_entry_that_would_exceed_the_budget_is_refused_not_evicted(self):
        """Admission control, not LRU eviction.

        A search walks the scan window in the same recency order every query, so
        the working set is cyclic. Evicting to make room for the newcomer would
        drop the entry needed one step later — the hit rate collapses to zero
        instead of degrading. Refusing the newcomer keeps whatever fits.
        """
        cache = self._cache(10)
        cache["a"] = "1234567890"  # exactly fills the budget

        cache["b"] = "x"

        assert cache.get("a") == "1234567890", "the entry that fit must survive"
        assert cache.get("b") is None, "the entry that did not fit is refused"
        assert cache.stats()["refused"] == 1

    def test_partial_fill_still_serves_the_sessions_that_fit(self):
        cache = self._cache(6)
        for name in ("a", "b", "c", "d"):
            cache[name] = "xxx"  # 3 bytes each: only two fit

        held = [n for n in ("a", "b", "c", "d") if cache.get(n) is not None]
        assert held == ["a", "b"], "the earliest (most recent sessions) stay cached"
        assert cache.stats()["bytes"] == 6

    def test_replacing_a_key_releases_the_old_cost_first(self):
        """A session being appended to must not inflate the accounting.

        Without releasing the previous value the byte total would grow on every
        rewrite of the same key, and the session would eventually be refused
        admission for its OWN new value while still counting against the budget.
        """
        cache = self._cache(10)
        cache["a"] = "12345"
        cache["a"] = "67890"

        assert cache.get("a") == "67890"
        assert cache.stats()["bytes"] == 5, "the old value's cost is released"
        assert cache.stats()["refused"] == 0

    def test_pop_releases_the_budget(self):
        cache = self._cache(10)
        cache["a"] = "1234567890"
        cache.pop("a", None)

        assert cache.stats()["bytes"] == 0
        cache["b"] = "1234567890"
        assert cache.get("b") is not None, "the freed budget is reusable"

    def test_pop_of_an_absent_key_does_not_corrupt_the_accounting(self):
        cache = self._cache(10)
        cache["a"] = "123"
        cache.pop("missing", None)

        assert cache.stats()["bytes"] == 3, "a miss must not subtract anything"

    def test_zero_budget_disables_the_bound(self):
        cache = self._cache(0)
        cache["a"] = "x" * 10_000
        assert cache.get("a") is not None
        assert cache.stats()["refused"] == 0


class TestSnippetSourceIsMemoized:
    """The snippet is 92% of a warm query unless its source is memoized.

    Matching is already cheap (the fold cache). What was not cheap: every
    returned row re-opened its file and re-parsed JSONL until the first hit.
    """

    def test_warm_query_builds_snippets_without_reading_any_file(
        self, tmp_path, monkeypatch
    ):
        log = ConversationLog(base_dir=tmp_path)
        _seed(log, sessions=3)
        log.search_sessions("deployment", 50)  # warm both memos

        reads: list[str] = []
        original = ConversationLog._iter_message_texts

        def counted(self, key: str):
            reads.append(key)
            return original(self, key)

        monkeypatch.setattr(ConversationLog, "_iter_message_texts", counted)

        results = log.search_sessions("deployment", 50)

        assert results, "the query must still return rows"
        assert all(r.get("snippet") for r in results), "each row still has its snippet"
        assert reads == [], "a warm query must not re-read any session file"

    def test_snippet_falls_back_to_the_file_when_the_memo_has_no_entry(
        self, tmp_path, monkeypatch
    ):
        """The budget may refuse admission, so the memo is an optimization only.

        With a zero-byte budget nothing is ever stored, which is the same state
        as a refused entry — and the snippet must still be produced.
        """
        log = ConversationLog(base_dir=tmp_path)
        _seed(log, sessions=2)
        monkeypatch.setattr(log, "_snippet_cache", history._SearchTextCache(1, lambda v: 10**9))

        results = log.search_sessions("deployment", 50)

        assert results
        assert all(r.get("snippet") for r in results), "fallback still yields snippets"

    def test_a_write_that_preserves_mtime_still_drops_the_snippet_memo(self, tmp_path):
        """The case the memo's own mtime check cannot see.

        Housekeeping writes restore the pre-write mtime (``_restore_mtime``) so
        consolidation does not float stale sessions to the top of
        ``list_sessions``. After one of those the file's mtime still matches what
        the memo recorded, so the mtime guard in :meth:`_snippet_texts` is blind
        and ``_invalidate_cache`` is the only thing standing between the user and
        a preview quoting text the session no longer contains.

        Asserted on the memo directly rather than through a snippet, because a
        content-changing rewrite bumps the mtime and would pass on the guard
        alone — which is exactly the false confidence this test exists to avoid.
        """
        log = ConversationLog(base_dir=tmp_path)
        log.append("s0", "user", "the original wording mentions deployment")
        log.search_sessions("deployment", 50)
        assert log._snippet_cache.get("s0") is not None, "precondition: memo warm"
        before = log._path("s0").stat().st_mtime

        log.mark_consolidated("s0", 1)

        assert log._path("s0").stat().st_mtime == before, (
            "precondition: this write path preserves mtime, so the guard is blind"
        )
        assert log._snippet_cache.get("s0") is None, (
            "the memo must be dropped by invalidation, not left to the mtime guard"
        )

    def test_no_stale_preview_survives_a_content_rewrite(self, tmp_path):
        """End-to-end check that the two defences together hold.

        This one passes on the mtime guard alone; the preserved-mtime case above
        is what pins the invalidation.
        """
        log = ConversationLog(base_dir=tmp_path)
        log.append("s0", "user", "the original wording mentions deployment")
        log.search_sessions("deployment", 50)  # memoize the original text

        log.rewrite_session("s0", [{"role": "user", "content": "replaced deployment text"}])

        results = log.search_sessions("deployment", 50)
        assert results, "the session still matches after the rewrite"
        assert "original wording" not in results[0]["snippet"], "no stale preview"
        assert "replaced" in results[0]["snippet"]

    def test_a_changed_mtime_falls_back_rather_than_serving_a_stale_snippet(
        self, tmp_path
    ):
        """Defence in depth behind ``_invalidate_cache``.

        If a write path ever forgets to drop the memo, the stored mtime must
        still stop the snippet from being served from it.
        """
        log = ConversationLog(base_dir=tmp_path)
        log.append("s0", "user", "first deployment wording")
        log.search_sessions("deployment", 50)

        stored = log._snippet_cache.get("s0")
        assert stored is not None, "precondition: the memo holds this session"
        # Poison the memo, keeping its (now wrong) mtime.
        log._snippet_cache["s0"] = (stored[0], ["poisoned deployment content"])
        # Make the file's mtime disagree with the memo's.
        path = log._path("s0")
        os.utime(path, (stored[0] + 10, stored[0] + 10))

        snippet = log._content_snippet("s0", "deployment")
        assert "poisoned" not in snippet, "a stale mtime must not be trusted"
        assert "first deployment wording" in snippet

    def test_unreadable_session_drops_both_memos_together(self, tmp_path):
        """The two memos are two views of one file; they must never disagree.

        A stat failure invalidates the fold, and leaving the snippet source
        behind would let a later query pair a fresh fold with a stale preview.
        """
        log = ConversationLog(base_dir=tmp_path)
        log.append("s0", "user", "deployment notes")
        log.search_sessions("deployment", 50)
        assert log._snippet_cache.get("s0") is not None

        log._path("s0").unlink()
        log._folded_content("s0")

        assert log._folded_cache.get("s0") is None
        assert log._snippet_cache.get("s0") is None, "both memos drop together"


class TestBudgetCountsRealBytesNotCharacters:
    """``len()`` would undercount the ceiling by up to 4x.

    CPython stores a ``str`` at the narrowest width its contents allow, so one
    character is 1 byte for latin-1, 2 for the BMP and 4 for astral planes. A
    ceiling counted in characters retains 168 MB of ASCII, 336 MB of CJK or
    671 MB of emoji at a nominal 160 MB — and CJK is the ordinary case for a
    non-English corpus, so this is not a pathological-input concern.
    """

    def test_wide_characters_consume_more_budget_than_narrow_ones(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        sizer = log._folded_cache._sizer

        narrow = sizer((0.0, 0, "a" * 1000))
        wide = sizer((0.0, 0, "\U0001f600" * 1000))

        assert wide > narrow * 3, (
            f"a 4-byte-per-char string must cost ~4x a 1-byte one, "
            f"got narrow={narrow} wide={wide}"
        )

    def test_a_wide_string_is_refused_where_the_same_length_of_ascii_fits(self):
        """The behavioural consequence: the ceiling holds regardless of script."""
        budget = "x" * 2000
        cache: history._SearchTextCache[str] = history._SearchTextCache(
            budget.__sizeof__(), lambda v: v.__sizeof__()
        )

        cache["ascii"] = "y" * 2000
        assert cache.get("ascii") is not None, "same-width content of equal length fits"

        cache.clear()
        cache["emoji"] = "\U0001f600" * 2000
        assert cache.get("emoji") is None, (
            "the same CHARACTER count of astral text is ~4x the bytes and must not fit"
        )

    def test_the_snippet_sizer_charges_for_its_list_container(self, tmp_path):
        """Many small strings make the list itself a real share of the cost."""
        log = ConversationLog(base_dir=tmp_path)
        sizer = log._snippet_cache._sizer

        texts = ["x" * 10 for _ in range(500)]
        charged = sizer((0.0, texts))
        strings_only = sum(t.__sizeof__() for t in texts)

        assert charged > strings_only, "the list container must be charged too"
        assert charged == strings_only + texts.__sizeof__()


class TestAdmissionControlIsNotAOneWayRatchet:
    """A full cache must not freeze on its first-fill set.

    Admission control avoids the LRU cliff, but on its own it means entries
    persist forever once admitted: sessions that age out of the scan window keep
    holding budget while every newly created session is refused. The newest and
    most-searched sessions would become exactly the cold ones, and warm latency
    would regress over process lifetime until a restart.
    """

    def test_retain_frees_entries_outside_the_live_set(self):
        cache: history._SearchTextCache[str] = history._SearchTextCache(100, len)
        cache["old"] = "x" * 40
        cache["new"] = "y" * 40

        dropped = cache.retain({"new"})

        assert dropped == 1
        assert cache.get("old") is None
        assert cache.get("new") is not None
        assert cache.stats()["bytes"] == 40, "the freed budget is accounted"

    def test_retain_resets_the_pressure_signal(self):
        cache: history._SearchTextCache[str] = history._SearchTextCache(10, len)
        cache["a"] = "x" * 10
        cache["b"] = "y"  # refused
        assert cache.refused_since_prune() == 1

        cache.retain({"a"})

        assert cache.refused_since_prune() == 0, "a prune clears the signal it acted on"
        assert cache.stats()["refused"] == 1, "the cumulative count survives for diagnosis"

    def test_a_session_that_left_the_window_stops_holding_budget(self, tmp_path, monkeypatch):
        """End-to-end: an aged-out session's budget becomes reusable.

        The valve fires from ``search_sessions`` only when a memo is refusing, and
        a refusal happens *during* the walk while the prune runs *before* it — so
        the release lands on the NEXT query, not the one that discovered the
        pressure. That one-query lag is deliberate and bounded: the prune needs
        the scan window, which is computed at the top of the walk, and a query
        costs ~20 ms against a keystroke-driven caller. What must not happen is
        the budget staying pinned forever, which is what this asserts.
        """
        log = ConversationLog(base_dir=tmp_path)
        monkeypatch.setattr(history, "_SEARCH_SCAN_WINDOW", 1)
        # Equal-length content so the budget freed by one exactly admits the other,
        # which is what proves the valve restored service rather than just freeing.
        log.append("older", "user", "mentions deployment nine")
        log.search_sessions("deployment", 50)
        assert log._folded_cache.get("older") is not None, "precondition: cached"

        # A newer session displaces the older one from a 1-session window.
        log.append("newer", "user", "mentions deployment ten!")
        # Shrink the budget so admitting "newer" is refused while "older" holds it.
        log._folded_cache._max_bytes = log._folded_cache.stats()["bytes"]

        log.search_sessions("deployment", 50)  # discovers the pressure
        assert log._folded_cache.refused_since_prune() > 0, "precondition: refusing"
        assert log._folded_cache.get("newer") is None, "precondition: newer was refused"

        log.search_sessions("deployment", 50)  # acts on it

        assert log._folded_cache.get("older") is None, (
            "a session outside the scan window must not keep holding budget"
        )
        assert log._folded_cache.get("newer") is not None, (
            "the freed budget must admit the session that is actually in the window"
        )

    def test_an_uncontended_cache_is_never_pruned(self, tmp_path, monkeypatch):
        """The scan is only worth paying under pressure."""
        log = ConversationLog(base_dir=tmp_path)
        _seed(log, sessions=3)
        log.search_sessions("deployment", 50)

        calls: list[int] = []
        original = history._SearchTextCache.retain

        def counted(self, live_keys):
            calls.append(1)
            return original(self, live_keys)

        monkeypatch.setattr(history._SearchTextCache, "retain", counted)
        log.search_sessions("deployment", 50)

        assert calls == [], "no refusals means no prune"

    def test_first_refusal_is_logged_once(self, caplog):
        """``refused`` in stats() is only diagnosable if something surfaces it."""
        cache: history._SearchTextCache[str] = history._SearchTextCache(
            10, len, "test-memo"
        )
        cache["a"] = "x" * 10

        with caplog.at_level("WARNING"):
            cache["b"] = "y"
            cache["c"] = "z"

        hits = [r for r in caplog.records if "test-memo" in r.getMessage()]
        assert len(hits) == 1, f"warn once, not per refusal; saw {len(hits)}"
        assert "ceiling" in hits[0].getMessage()
