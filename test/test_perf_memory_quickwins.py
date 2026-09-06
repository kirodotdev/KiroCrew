"""Regression tests for the memory-store performance quick wins (batch 6).

Each test is written to FAIL if its corresponding fix is reverted:

- ``TestEpisodicBatchFetch`` — the FAISS search path resolves all hits in one
  ``IN (...)`` query that excludes the embedding BLOB (was one ``SELECT *``
  per hit).
- ``TestStorePragmas`` — ``memory.db`` runs WAL and keeps ``synchronous=FULL``
  (a durability guard against relaxing it to ``NORMAL``).
- ``TestLastAccessedDebounce`` — repeated searches within the debounce window
  issue no further ``last_accessed_at`` writes.
- ``TestLessonsSingleQuery`` — context assembly calls ``get_lessons_context()``
  once instead of probing with ``get_lessons()`` first.
- ``TestRecentFromSourceTailRead`` — ``recent_from_source`` reads a bounded
  tail rather than the whole transcript.
- ``TestEpisodicSqliteCosineNumpy`` — the sqlite episodic tier scores rows with
  one numpy mat-vec when numpy is available (issue #8548), giving identical
  results to the stdlib loop; ``test_numpy_branch_is_actually_taken`` fails if
  the vectorized branch is reverted.
- ``TestEpisodicScoringCacheReuse`` — that same tier holds its scoring columns
  resident (issue #8894), so a second search with no write in between repeats
  neither the full-population fetch nor the per-row candidate build, and every
  writer that changes the scored population invalidates the set.
"""

from __future__ import annotations

import builtins
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.history import ConversationLog
from kiro_crew.vector_memory import _HAS_FAISS, _HAS_NUMPY, VectorMemoryStore


def _fake_embed(dim: int):
    """Deterministic pseudo-embedding so FAISS has real vectors, no network."""

    def _embed(text: str) -> list[float]:
        seed = sum(ord(c) for c in text)
        return [float((seed + i) % 7) + 0.1 for i in range(dim)]

    return _embed


class _SqlRecorder:
    """Collects every statement sqlite executes on a connection."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def __call__(self, sql: str) -> None:
        self.statements.append(" ".join(sql.split()))

    def matching(self, *needles: str) -> list[str]:
        return [s for s in self.statements if all(n in s for n in needles)]


def _faiss_store(tmp_path: Path, n_entries: int = 12, dim: int = 16) -> VectorMemoryStore:
    store = VectorMemoryStore(db_path=tmp_path / "mem.db", embedding_dim=dim)
    store.init()
    store.embed_fn = _fake_embed(dim)
    store.build_faiss_index()
    for i in range(n_entries):
        store.write_episodic(f"episodic memory number {i} about topic alpha beta")
    return store


class TestEpisodicBatchFetch:
    def test_faiss_hits_resolved_in_one_query(self, tmp_path: Path) -> None:
        """N hits cost ONE SELECT, not one per hit."""
        if not (_HAS_FAISS and _HAS_NUMPY):
            pytest.skip("FAISS/numpy not available on this platform")
        store = _faiss_store(tmp_path)
        recorder = _SqlRecorder()
        store.db.set_trace_callback(recorder)
        try:
            results = store.search_episodic(
                query_embedding=_fake_embed(16)("topic alpha"),
                query_text="topic alpha",
                limit=5,
            )
        finally:
            store.db.set_trace_callback(None)

        assert results, "expected episodic hits"
        selects = recorder.matching("SELECT", "FROM episodic_memories")
        # Pre-fix this was `limit * 2` separate `SELECT * ... WHERE id = ?`
        # statements (10 for limit=5 with 12 rows indexed).
        assert len(selects) == 1, f"expected a single batched SELECT, got {selects}"
        assert " IN (" in selects[0]

    def test_batched_select_excludes_embedding_blob(self, tmp_path: Path) -> None:
        """Search results never carry the embedding column."""
        if not (_HAS_FAISS and _HAS_NUMPY):
            pytest.skip("FAISS/numpy not available on this platform")
        store = _faiss_store(tmp_path)
        results = store.search_episodic(
            query_embedding=_fake_embed(16)("topic alpha"),
            query_text="topic alpha",
            limit=5,
        )
        assert results
        for r in results:
            assert "embedding" not in r, "embedding BLOB leaked into a search result"
        # The useful fields are all still present.
        assert {"id", "text", "importance", "created_at", "score", "cosine_sim"} <= set(results[0])

    def test_tombstoned_rows_are_excluded(self, tmp_path: Path) -> None:
        """A tombstoned row indexed in FAISS is dropped by the batch fetch."""
        if not (_HAS_FAISS and _HAS_NUMPY):
            pytest.skip("FAISS/numpy not available on this platform")
        store = _faiss_store(tmp_path)
        victim = store.db.execute(
            "SELECT id FROM episodic_memories WHERE is_deleted = 0 LIMIT 1"
        ).fetchone()["id"]
        store._delete_episodic_row(victim)
        results = store.search_episodic(
            query_embedding=_fake_embed(16)("topic alpha"),
            query_text="topic alpha",
            limit=10,
        )
        assert victim not in {r["id"] for r in results}

    def test_get_episodic_batch_empty_input(self, tmp_path: Path) -> None:
        store = VectorMemoryStore(db_path=tmp_path / "mem.db")
        store.init()
        assert store._get_episodic_batch([]) == {}


class TestStorePragmas:
    def test_wal_with_full_synchronous(self, tmp_path: Path) -> None:
        """memory.db runs WAL but keeps the default FULL synchronous setting.

        This is a durability guard, not a perf assertion. Relaxing to NORMAL (1)
        drops the per-commit fsync, and under WAL that is only crash-safe across
        a process crash -- an OS crash or power loss can lose the unsynced WAL
        tail, which here means acknowledged memories and lessons. Write volume
        is reduced by debouncing the last_accessed_at touch instead.
        """
        store = VectorMemoryStore(db_path=tmp_path / "mem.db")
        store.init()
        journal = store.db.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(journal).lower() == "wal"
        # 2 == FULL, the sqlite default. Must not be relaxed to 1 (NORMAL).
        assert store.db.execute("PRAGMA synchronous").fetchone()[0] == 2


class TestLastAccessedDebounce:
    def test_repeat_search_skips_last_accessed_write(self, tmp_path: Path) -> None:
        """A second search inside the debounce window issues no UPDATE."""
        if not (_HAS_FAISS and _HAS_NUMPY):
            pytest.skip("FAISS/numpy not available on this platform")
        store = _faiss_store(tmp_path)
        embed = _fake_embed(16)

        first = store.search_episodic(
            query_embedding=embed("topic alpha"), query_text="topic alpha", limit=5
        )
        assert first
        # First search persisted the timestamp.
        row = store.db.execute(
            "SELECT last_accessed_at FROM episodic_memories WHERE id = ?", (first[0]["id"],)
        ).fetchone()
        assert row["last_accessed_at"] is not None

        recorder = _SqlRecorder()
        store.db.set_trace_callback(recorder)
        try:
            for _ in range(3):
                store.search_episodic(
                    query_embedding=embed("topic alpha"), query_text="topic alpha", limit=5
                )
        finally:
            store.db.set_trace_callback(None)

        updates = recorder.matching("UPDATE episodic_memories SET last_accessed_at")
        assert updates == [], f"expected debounced touches, got {len(updates)} UPDATEs"

    def test_touch_resumes_after_debounce_window(self, tmp_path: Path) -> None:
        """Debouncing is time-bounded, not a permanent suppression."""
        if not (_HAS_FAISS and _HAS_NUMPY):
            pytest.skip("FAISS/numpy not available on this platform")
        store = _faiss_store(tmp_path)
        embed = _fake_embed(16)
        store.search_episodic(
            query_embedding=embed("topic alpha"), query_text="topic alpha", limit=5
        )
        # Age every recorded touch past the window.
        store._last_accessed_touch = {
            k: v - (store._LAST_ACCESSED_DEBOUNCE_SECS + 1)
            for k, v in store._last_accessed_touch.items()
        }
        recorder = _SqlRecorder()
        store.db.set_trace_callback(recorder)
        try:
            store.search_episodic(
                query_embedding=embed("topic alpha"), query_text="topic alpha", limit=5
            )
        finally:
            store.db.set_trace_callback(None)
        assert recorder.matching("UPDATE episodic_memories SET last_accessed_at")

    def test_sqlite_fallback_path_also_debounces(self, tmp_path: Path) -> None:
        """The no-FAISS cosine fallback shares the debounced touch helper."""
        dim = 16
        store = VectorMemoryStore(db_path=tmp_path / "mem.db", embedding_dim=dim)
        store.init()
        store.embed_fn = _fake_embed(dim)
        for i in range(5):
            store.write_episodic(f"episodic memory number {i} about topic alpha beta")
        q = _fake_embed(dim)("topic alpha")
        # Force the stdlib fallback regardless of whether FAISS is installed.
        store._faiss_index = None
        assert store._sqlite_vector_search(q, "topic alpha", 5)

        recorder = _SqlRecorder()
        store.db.set_trace_callback(recorder)
        try:
            store._sqlite_vector_search(q, "topic alpha", 5)
        finally:
            store.db.set_trace_callback(None)
        assert recorder.matching("UPDATE episodic_memories SET last_accessed_at") == []


class TestLessonsSingleQuery:
    def _builder(self, tmp_path: Path):
        from kiro_crew.context import ContextBuilder, LessonStore, MemoryStore, SkillsLoader

        return ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
            lessons=LessonStore(base_dir=tmp_path),
        )

    def test_lessons_probe_query_is_gone(self, tmp_path: Path) -> None:
        """get_lessons() is no longer used as an emptiness probe."""
        from kiro_crew.context import ContextBuilder

        vector_store = MagicMock()
        vector_store.get_lessons_context.return_value = (
            "[Learned corrections]\n- always run the formatter\n[End of learned corrections]\n"
        )
        vector_store.get_semantic_context.return_value = ""
        vector_store.get_episodic_context.return_value = ""
        fake_memory = MagicMock()
        fake_memory.vector_store = vector_store
        fake_memory.get_context.return_value = ""

        builder = self._builder(tmp_path)
        with patch.object(ContextBuilder, "get_memory_for", return_value=fake_memory):
            ctx = builder.build_session_context(session_key="sess-lessons")

        assert "always run the formatter" in ctx
        vector_store.get_lessons.assert_not_called()
        assert vector_store.get_lessons_context.call_count == 1

    def test_file_lessons_answer_only_when_there_is_no_vector_store(
        self, tmp_path: Path
    ) -> None:
        """The file store is the fallback for HAVING no vector store.

        It is deliberately NOT the fallback for a vector store that returned
        nothing. Scope filtering gave "empty" a second meaning -- it also means
        "every lesson is out of scope here" -- so answering from the file store on
        empty would let it speak for a live vector store and re-inject rows that
        were deleted from it.
        """
        from kiro_crew.context import ContextBuilder
        from kiro_crew.learn import Lesson, LessonStore

        lessons = LessonStore(base_dir=tmp_path)
        lessons.save(Lesson(ts="1", rule="never force push to mainline", category="knowledge"))

        # No vector store: the file store answers.
        no_vs = MagicMock()
        no_vs.vector_store = None
        no_vs.get_context.return_value = ""
        builder = self._builder(tmp_path)
        builder.lessons = lessons
        with patch.object(ContextBuilder, "get_memory_for", return_value=no_vs):
            ctx = builder.build_session_context(session_key="sess-lessons-no-vs")
        assert "never force push to mainline" in ctx

        # Live vector store returning nothing: the file store stays silent.
        vector_store = MagicMock()
        vector_store.get_lessons_context.return_value = ""
        vector_store.get_semantic_context.return_value = ""
        vector_store.get_episodic_context.return_value = ""
        with_vs = MagicMock()
        with_vs.vector_store = vector_store
        with_vs.get_context.return_value = ""
        builder2 = self._builder(tmp_path)
        builder2.lessons = lessons
        with patch.object(ContextBuilder, "get_memory_for", return_value=with_vs):
            ctx2 = builder2.build_session_context(session_key="sess-lessons-empty")
        assert "never force push to mainline" not in ctx2
        assert vector_store.get_lessons_context.call_count == 1


class _ByteCountingOpen:
    """builtins.open wrapper that tallies bytes read from watched paths."""

    def __init__(self, watched: Path) -> None:
        self._real = builtins.open
        self._watched = str(watched)
        self.bytes_read = 0

    def __call__(self, file, *args, **kwargs):  # type: ignore[no-untyped-def]
        handle = self._real(file, *args, **kwargs)
        if str(file) != self._watched:
            return handle
        outer = self

        class _Counting:
            def __init__(self, inner) -> None:  # type: ignore[no-untyped-def]
                self._inner = inner

            def read(self, *a, **k):  # type: ignore[no-untyped-def]
                data = self._inner.read(*a, **k)
                outer.bytes_read += len(data)
                return data

            def readline(self, *a, **k):  # type: ignore[no-untyped-def]
                data = self._inner.readline(*a, **k)
                outer.bytes_read += len(data)
                return data

            def __iter__(self):  # type: ignore[no-untyped-def]
                for line in self._inner:
                    outer.bytes_read += len(line)
                    yield line

            def __enter__(self):  # type: ignore[no-untyped-def]
                self._inner.__enter__()
                return self

            def __exit__(self, *exc):  # type: ignore[no-untyped-def]
                return self._inner.__exit__(*exc)

            def __getattr__(self, name):  # type: ignore[no-untyped-def]
                return getattr(self._inner, name)

        return _Counting(handle)


class TestRecentFromSourceTailRead:
    #: Messages written to the fixture transcript. Large enough that a
    #: whole-file read is unmistakably distinguishable from a tail read.
    _N_MESSAGES = 3000

    def _write_transcript(self, log: ConversationLog, key: str) -> Path:
        path = log._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        filler = "x" * 400
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"_type": "metadata", "tab_id": "t1"}) + "\n")
            for i in range(self._N_MESSAGES):
                f.write(
                    json.dumps(
                        {
                            "role": "user" if i % 2 == 0 else "assistant",
                            "content": f"msg-{i} {filler}",
                            "ts": f"2026-01-01T00:00:{i:04d}",
                        }
                    )
                    + "\n"
                )
        return path

    def test_reads_bounded_tail_not_whole_file(self, tmp_path: Path) -> None:
        log = ConversationLog(base_dir=tmp_path)
        path = self._write_transcript(log, "slack-C1-alpha")
        total = path.stat().st_size
        assert total > 1_000_000, "fixture must be large enough to show the difference"

        counter = _ByteCountingOpen(path)
        with patch.object(builtins, "open", counter):
            msgs = log.recent_from_source("slack-C1", max_messages=20)

        assert len(msgs) == 20
        assert msgs[-1]["content"].startswith(f"msg-{self._N_MESSAGES - 1} ")
        # Bounded tail window (~51 KB) plus a 5-line head probe. The pre-fix
        # implementation read the entire file.
        assert counter.bytes_read < 200_000, (
            f"read {counter.bytes_read} of {total} bytes — expected a bounded tail read"
        )

    def test_restricted_sessions_still_skipped(self, tmp_path: Path) -> None:
        log = ConversationLog(base_dir=tmp_path)
        path = log._path("slack-C2-secret")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"_type": "metadata", "memory_mode": "incognito"}) + "\n")
            f.write(
                json.dumps({"role": "user", "content": "secret", "ts": "2026-01-01T00:00:00"})
                + "\n"
            )
        assert log.recent_from_source("slack-C2", max_messages=20) == []

    def test_excludes_named_key_and_orders_by_timestamp(self, tmp_path: Path) -> None:
        log = ConversationLog(base_dir=tmp_path)
        for name, stamp in (("slack-C3-a", "01"), ("slack-C3-b", "02")):
            path = log._path(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(
                    json.dumps(
                        {
                            "role": "user",
                            "content": f"from-{name}",
                            "ts": f"2026-01-{stamp}T00:00:00",
                        }
                    )
                    + "\n"
                )
        msgs = log.recent_from_source("slack-C3", exclude_key="slack-C3-a", max_messages=20)
        assert [m["content"] for m in msgs] == ["from-slack-C3-b"]


class TestEpisodicSqliteCosineNumpy:
    """The sqlite episodic cosine scan gives identical results on both branches.

    Issue #8548: `_sqlite_vector_search` is the tier a default install runs
    (faiss-cpu is not a declared dependency), and it scored rows with a pure
    Python loop despite numpy being available. The numpy branch must be a
    drop-in: same ids, same order, same cosine values as the stdlib loop.
    """

    DIM = 8

    @staticmethod
    def _normed(seed: int, dim: int) -> list[float]:
        """Deterministic pre-normalized vector (matches the storage contract)."""
        import math as _math

        raw = [float((seed + i) % 5) + 0.25 for i in range(dim)]
        norm = _math.sqrt(sum(x * x for x in raw))
        return [x / norm for x in raw]

    def _seed_store(self, tmp_path: Path) -> "VectorMemoryStore":
        import struct as _struct
        from datetime import datetime, timezone

        store = VectorMemoryStore(db_path=tmp_path / "mem.db", embedding_dim=self.DIM)
        store.init()
        now = datetime.now(tz=timezone.utc).isoformat()
        rows = [
            ("ep-1", self._normed(1, self.DIM), ["alpha"]),
            ("ep-2", self._normed(3, self.DIM), ["beta"]),
            ("ep-3", self._normed(7, self.DIM), ["alpha", "beta"]),
            ("ep-4", self._normed(11, self.DIM), []),
        ]
        for mem_id, vec, tags in rows:
            store.db.execute(
                "INSERT INTO episodic_memories "
                "(id, conversation_id, text, embedding, tags, importance, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    mem_id,
                    "conv-1",
                    f"episodic row {mem_id}",
                    _struct.pack(f"{self.DIM}f", *vec),
                    json.dumps(tags),
                    0.6,
                    now,
                ),
            )
        # One row whose embedding length does NOT match the query dim: both
        # branches must skip it.
        short = self._normed(5, self.DIM - 3)
        store.db.execute(
            "INSERT INTO episodic_memories "
            "(id, conversation_id, text, embedding, tags, importance, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "ep-short",
                "conv-1",
                "episodic row with a mismatched embedding length",
                _struct.pack(f"{self.DIM - 3}f", *short),
                "[]",
                0.6,
                now,
            ),
        )
        store.db.commit()
        return store

    def _search(
        self,
        store: "VectorMemoryStore",
        monkeypatch: pytest.MonkeyPatch,
        use_numpy: bool,
        tag_filter: list[str] | None = None,
    ) -> list[dict]:
        import kiro_crew.vector_memory as vm

        monkeypatch.setattr(vm, "_HAS_NUMPY", use_numpy)
        return store._sqlite_vector_search(
            query_embedding=self._normed(2, self.DIM),
            query_text="episodic row",
            limit=10,
            mmr=False,
            tag_filter=tag_filter,
        )

    def test_numpy_branch_matches_stdlib_branch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        if not _HAS_NUMPY:
            pytest.skip("numpy not available on this platform")
        store = self._seed_store(tmp_path)
        fast = self._search(store, monkeypatch, use_numpy=True)
        slow = self._search(store, monkeypatch, use_numpy=False)

        assert [r["id"] for r in fast] == [r["id"] for r in slow]
        assert [r["id"] for r in fast]  # non-empty: the fixture rows survived
        for f, s in zip(fast, slow):
            assert abs(f["cosine_sim"] - s["cosine_sim"]) < 1e-6
            assert abs(f["score"] - s["score"]) < 1e-6

    def test_numpy_branch_is_actually_taken(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reverting the vectorization makes this fail: the numpy path must not
        touch ``struct.unpack``, which is exactly what the old per-row loop did."""
        if not _HAS_NUMPY:
            pytest.skip("numpy not available on this platform")
        import kiro_crew.vector_memory as vm

        store = self._seed_store(tmp_path)

        def _boom(*args: object, **kwargs: object) -> None:
            raise AssertionError("struct.unpack called on the numpy branch")

        monkeypatch.setattr(vm.struct, "unpack", _boom)
        results = self._search(store, monkeypatch, use_numpy=True)
        assert {r["id"] for r in results} == {"ep-1", "ep-2", "ep-3", "ep-4"}

    def test_mismatched_embedding_length_skipped_in_both_branches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        if not _HAS_NUMPY:
            pytest.skip("numpy not available on this platform")
        store = self._seed_store(tmp_path)
        for use_numpy in (True, False):
            ids = {r["id"] for r in self._search(store, monkeypatch, use_numpy=use_numpy)}
            assert "ep-short" not in ids
            assert {"ep-1", "ep-2", "ep-3", "ep-4"} <= ids

    def test_tag_filter_applies_in_both_branches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        if not _HAS_NUMPY:
            pytest.skip("numpy not available on this platform")
        store = self._seed_store(tmp_path)
        for use_numpy in (True, False):
            ids = {
                r["id"]
                for r in self._search(store, monkeypatch, use_numpy=use_numpy, tag_filter=["alpha"])
            }
            assert ids == {"ep-1", "ep-3"}

    def test_zero_surviving_rows_returns_empty_in_both_branches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        if not _HAS_NUMPY:
            pytest.skip("numpy not available on this platform")
        store = self._seed_store(tmp_path)
        for use_numpy in (True, False):
            ids = {
                r["id"]
                for r in self._search(
                    store, monkeypatch, use_numpy=use_numpy, tag_filter=["no-such-tag"]
                )
            }
            assert ids == set()


class TestEpisodicScoringCacheReuse:
    """Issue #8894: the sqlite episodic tier keeps its scoring columns resident.

    The scored population changes only when something writes it, so two
    identical searches with nothing in between must repeat neither the
    full-population fetch nor the per-row candidate build. Every assertion here
    is on the SHAPE of the work (which statements run, which helpers are
    called), never on a duration: a timed ratio false-reds on a shared CI
    runner.

    The correctness half is the harder half, and it is what the rest of the
    class pins: a write of any kind between two searches must be visible to the
    second one, including the three cases a naive append-only cache misses (the
    backfill on a no-FAISS install, the re-embed reset, and a second process
    writing the same file).
    """

    DIM = 8
    #: Matches the full-population scan whether it comes from the legacy
    #: per-call fetch or from the cache build — so the "no refetch" assertion
    #: fails if the cache is reverted rather than silently passing.
    POPULATION_SCAN = ("SELECT", "FROM episodic_memories", "embedding IS NOT NULL")

    def _store(self, tmp_path: Path, n_entries: int = 6) -> VectorMemoryStore:
        """A store pinned to the sqlite tier (no FAISS index)."""
        store = VectorMemoryStore(db_path=tmp_path / "mem.db", embedding_dim=self.DIM)
        store.init()
        store.embed_fn = _fake_embed(self.DIM)
        store._faiss_index = None
        store._faiss_id_map = []
        for i in range(n_entries):
            store.write_episodic(f"episodic memory number {i} about topic alpha beta")
        return store

    def _query(self) -> list[float]:
        return _fake_embed(self.DIM)("topic alpha")

    def _search(self, store: VectorMemoryStore, limit: int = 10) -> list[dict]:
        return store._sqlite_vector_search(self._query(), "topic alpha", limit)

    def test_second_identical_search_refetches_no_rows(self, tmp_path: Path) -> None:
        """The population scan runs once, not once per call."""
        if not _HAS_NUMPY:
            pytest.skip("numpy not available on this platform")
        store = self._store(tmp_path)
        assert self._search(store), "expected episodic hits to warm the scoring set"

        recorder = _SqlRecorder()
        store.db.set_trace_callback(recorder)
        try:
            second = self._search(store)
        finally:
            store.db.set_trace_callback(None)

        assert second, "the cached path must still return the same hits"
        assert recorder.matching(*self.POPULATION_SCAN) == [], (
            "a second identical search re-scanned the whole population: "
            f"{recorder.matching(*self.POPULATION_SCAN)}"
        )

    def test_cached_path_does_not_build_a_candidate_per_row(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Decay scoring is vectorized, not a Python dict build per row.

        ``_episodic_candidate`` is the per-row builder the legacy path calls for
        every surviving row. Computing the decay row by row is what put 40% of
        the call in that phase, so the cached path must not reach it at all.
        """
        if not _HAS_NUMPY:
            pytest.skip("numpy not available on this platform")
        store = self._store(tmp_path)
        assert self._search(store)

        def _boom(*args: object, **kwargs: object) -> None:
            raise AssertionError("per-row candidate build on the cached path")

        monkeypatch.setattr(VectorMemoryStore, "_episodic_candidate", _boom)
        assert self._search(store), "the cached path must return hits without _episodic_candidate"

    def test_write_between_searches_produces_fresh_results(self, tmp_path: Path) -> None:
        """An in-process write is visible to the next search."""
        if not _HAS_NUMPY:
            pytest.skip("numpy not available on this platform")
        store = self._store(tmp_path)
        before = self._search(store)
        assert before

        assert store.write_episodic("a further episodic memory about topic alpha beta gamma")
        after = self._search(store)

        assert len(after) == len(before) + 1
        assert {r["id"] for r in before} < {r["id"] for r in after}

    def test_tombstone_between_searches_produces_fresh_results(self, tmp_path: Path) -> None:
        """A delete is visible to the next search."""
        if not _HAS_NUMPY:
            pytest.skip("numpy not available on this platform")
        store = self._store(tmp_path)
        before = self._search(store)
        assert len(before) > 1

        assert store.delete_episodic(before[0]["id"])
        after = self._search(store)

        assert before[0]["id"] not in {r["id"] for r in after}
        assert len(after) == len(before) - 1

    def test_backfill_is_visible_without_faiss(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The backfill rebuilds FAISS only under ``_HAS_FAISS``; the cache is not.

        A row embedded by the sweep is a row the population scan never saw. A
        body lookup cannot rescue this — it drops ids that vanished but can
        never surface ids that appeared — so recall would degrade silently on
        exactly the install this tier serves.
        """
        if not _HAS_NUMPY:
            pytest.skip("numpy not available on this platform")
        import kiro_crew.vector_memory as vm

        monkeypatch.setattr(vm, "_HAS_FAISS", False)
        store = self._store(tmp_path)
        assert store.write_episodic(
            "a deferred episodic memory about topic alpha beta", defer_embedding=True
        )
        before = self._search(store)
        assert before

        assert store.backfill_missing_embeddings(pace=False) == 1
        after = self._search(store)

        assert len(after) == len(before) + 1

    def test_reembed_reset_clears_the_scoring_set(self, tmp_path: Path) -> None:
        """``reconcile_embedding_space`` NULLs every vector; the cache goes with them."""
        if not _HAS_NUMPY:
            pytest.skip("numpy not available on this platform")
        store = self._store(tmp_path)
        assert self._search(store)

        store.reconcile_embedding_space("some-other-model-signature", clear_when_unknown=True)

        assert self._search(store) == []

    def test_another_connection_insert_is_visible(self, tmp_path: Path) -> None:
        """A second process writing the same store must not be served stale.

        The in-process consistency gate cannot see it, so the cache is keyed on
        ``PRAGMA data_version`` as well: it moves when ANOTHER connection
        commits.
        """
        if not _HAS_NUMPY:
            pytest.skip("numpy not available on this platform")
        import sqlite3 as _sqlite3
        import struct as _struct
        from datetime import datetime, timezone

        store = self._store(tmp_path)
        before = self._search(store)
        assert before

        other = _sqlite3.connect(str(tmp_path / "mem.db"))
        try:
            other.execute(
                "INSERT INTO episodic_memories "
                "(id, conversation_id, text, embedding, tags, importance, created_at, is_deleted) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
                (
                    "from-another-process",
                    "conv-other",
                    "an episodic memory written by another process about topic alpha",
                    _struct.pack(f"{self.DIM}f", *self._query()),
                    "[]",
                    0.5,
                    datetime.now(tz=timezone.utc).isoformat(),
                ),
            )
            other.commit()
        finally:
            other.close()

        after = self._search(store)
        assert "from-another-process" in {r["id"] for r in after}

    def test_every_episodic_writer_invalidates_the_scoring_set(self) -> None:
        """Ratchet: a new writer cannot land without covering the cache.

        Missing an invalidation degrades recall with no error and no failing
        test, so the guard is structural rather than a list of cases someone
        remembers to extend.
        """
        import ast

        src_root = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
        write_verbs = (
            "INSERT INTO episodic_memories",
            "UPDATE episodic_memories",
            "DELETE FROM episodic_memories",
        )
        # Writers that provably touch no cached column. `last_accessed_at` is
        # resolved per search from the winners' row bodies, never from the
        # cache, so debouncing it must not drop the scoring set.
        exempt = {"_touch_last_accessed"}
        offenders: list[str] = []

        for path in sorted(src_root.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            if not any(verb in text for verb in write_verbs):
                continue
            for node in ast.walk(ast.parse(text)):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if node.name in exempt:
                    continue
                body = ast.dump(node)
                if not any(verb in body for verb in write_verbs):
                    continue
                if "_invalidate_episodic_scoring" in body:
                    continue
                offenders.append(f"{path.relative_to(src_root)}::{node.name}")

        assert not offenders, (
            "these functions write episodic_memories without invalidating the "
            f"resident scoring set: {offenders}"
        )


class TestEpisodicCachedRankingParity:
    """The resident scoring set must not change what a search returns.

    Filtering runs across the FULL population before ``limit`` — a tag matching
    few rows, or a relevance gate admitting few, must still return those rows
    rather than whatever fell inside a top-k window. So parity is checked with
    the filters ON, against the stdlib branch, which reads every row from
    sqlite on every call and shares no code with the cache.
    """

    DIM = 8

    @staticmethod
    def _normed(seed: int, dim: int) -> list[float]:
        import math as _math

        raw = [float((seed + i) % 5) + 0.25 for i in range(dim)]
        norm = _math.sqrt(sum(x * x for x in raw))
        return [x / norm for x in raw]

    def _seed(self, tmp_path: Path) -> VectorMemoryStore:
        import struct as _struct
        from datetime import datetime, timedelta, timezone

        store = VectorMemoryStore(db_path=tmp_path / "mem.db", embedding_dim=self.DIM)
        store.init()
        store._faiss_index = None
        now = datetime.now(tz=timezone.utc)
        rows = [
            ("ep-1", 1, ["alpha"], 0.9, 0, "short row one"),
            ("ep-2", 3, ["beta"], 0.2, 40, "short row two"),
            ("ep-3", 7, ["alpha", "beta"], 0.6, 5, "a longer row " + "padding " * 45),
            ("ep-4", 11, [], 0.5, 400, "short row four"),
            ("ep-5", 2, ["alpha"], 0.1, 1, "short row five"),
        ]
        for mem_id, seed, tags, importance, days, text in rows:
            store.db.execute(
                "INSERT INTO episodic_memories "
                "(id, conversation_id, text, embedding, tags, importance, created_at, is_deleted) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
                (
                    mem_id,
                    "conv-1",
                    text,
                    _struct.pack(f"{self.DIM}f", *self._normed(seed, self.DIM)),
                    json.dumps(tags),
                    importance,
                    (now - timedelta(days=days)).isoformat(),
                ),
            )
        store.db.commit()
        return store

    def _run(
        self,
        store: VectorMemoryStore,
        monkeypatch: pytest.MonkeyPatch,
        *,
        use_numpy: bool,
        **kwargs: object,
    ) -> list[dict]:
        import kiro_crew.vector_memory as vm

        monkeypatch.setattr(vm, "_HAS_NUMPY", use_numpy)
        return store._sqlite_vector_search(
            query_embedding=self._normed(2, self.DIM),
            query_text="row",
            limit=3,
            **kwargs,  # type: ignore[arg-type]
        )

    @pytest.mark.parametrize(
        "kwargs",
        [
            {},
            {"mmr": False},
            {"tag_filter": ["alpha"]},
            {"relevance_filter": True},
            {"mmr": False, "relevance_filter": True, "tag_filter": ["beta"]},
        ],
    )
    def test_cached_matches_stdlib(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kwargs: dict
    ) -> None:
        if not _HAS_NUMPY:
            pytest.skip("numpy not available on this platform")
        store = self._seed(tmp_path)
        cached = self._run(store, monkeypatch, use_numpy=True, **kwargs)
        # Warm, then run again: the second call is the one served from the cache.
        cached_again = self._run(store, monkeypatch, use_numpy=True, **kwargs)
        stdlib = self._run(store, monkeypatch, use_numpy=False, **kwargs)

        assert [r["id"] for r in cached] == [r["id"] for r in stdlib]
        assert [r["id"] for r in cached_again] == [r["id"] for r in stdlib]
        for got, want in zip(cached_again, stdlib):
            assert got.keys() == want.keys()
            assert abs(got["cosine_sim"] - want["cosine_sim"]) < 1e-6
            assert abs(got["score"] - want["score"]) < 1e-6
            assert got["text"] == want["text"]
            assert got["tags"] == want["tags"]


class TestOverBudgetRefusalIsMemoized:
    """An over-budget store must not pay the build scan on every search.

    Design finding on #8956: `_build_episodic_scoring_set` returning None
    (population over `_EPISODIC_SCORING_MAX_BYTES`) was not memoized, so every
    search first full-scanned the population trying to build, then full-scanned
    again to answer per-call — strictly worse than the pre-cache baseline. The
    refusal is now memoized under the same (dim, generation, data_version)
    tokens as a successful build: settled between writes, re-probed after one.
    """

    DIM = 8
    #: The BUILD scan is distinguishable from the per-call answer scan: only the
    #: build selects the computed text-length column.
    BUILD_SCAN = ("text_len", "FROM episodic_memories")

    def _over_budget_store(self, tmp_path: Path, monkeypatch) -> VectorMemoryStore:
        import kiro_crew.vector_memory as vm_mod

        # Smaller than a single 8-float embedding blob, so any populated store
        # refuses to build.
        monkeypatch.setattr(vm_mod, "_EPISODIC_SCORING_MAX_BYTES", 16)
        store = VectorMemoryStore(db_path=tmp_path / "mem.db", embedding_dim=self.DIM)
        store.init()
        store.embed_fn = _fake_embed(self.DIM)
        store._faiss_index = None
        store._faiss_id_map = []
        for i in range(4):
            store.write_episodic(f"episodic memory number {i} about topic alpha beta")
        return store

    def _search(self, store: VectorMemoryStore) -> list[dict]:
        return store._sqlite_vector_search(_fake_embed(self.DIM)("topic alpha"), "topic alpha", 10)

    def test_refused_build_is_not_retried_per_search(self, tmp_path: Path, monkeypatch) -> None:
        if not _HAS_NUMPY:
            pytest.skip("numpy not available on this platform")
        store = self._over_budget_store(tmp_path, monkeypatch)
        assert self._search(store), "over-budget store must still answer via the per-call read"

        recorder = _SqlRecorder()
        store.db.set_trace_callback(recorder)
        try:
            assert self._search(store)
        finally:
            store.db.set_trace_callback(None)

        assert recorder.matching(*self.BUILD_SCAN) == [], (
            "an over-budget store re-ran the build scan on a later search: "
            f"{recorder.matching(*self.BUILD_SCAN)}"
        )

    def test_a_write_reopens_the_probe(self, tmp_path: Path, monkeypatch) -> None:
        """The memo lives exactly as long as a successful build would."""
        if not _HAS_NUMPY:
            pytest.skip("numpy not available on this platform")
        store = self._over_budget_store(tmp_path, monkeypatch)
        assert self._search(store)
        assert self._search(store)

        assert store.write_episodic("a new memory about topic alpha")

        recorder = _SqlRecorder()
        store.db.set_trace_callback(recorder)
        try:
            assert self._search(store)
        finally:
            store.db.set_trace_callback(None)

        assert recorder.matching(*self.BUILD_SCAN), (
            "after a write the store must re-probe whether the population now fits"
        )
