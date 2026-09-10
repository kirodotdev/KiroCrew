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
  one numpy mat-vec when numpy is available, giving identical
  results to the stdlib loop; ``test_numpy_branch_is_actually_taken`` fails if
  the vectorized branch is reverted.
- ``TestEpisodicScoringCacheReuse`` — that same tier holds its scoring columns
  resident, so a second search with no write in between repeats
  neither the full-population fetch nor the per-row candidate build, and every
  writer that changes the scored population invalidates the set.
- ``TestSqliteVectorSearchNumpyRung`` — that tier produces the SAME ranking and
  cosines with numpy present as without it, over a fixture written through the
  real embed-and-admit path, and its numpy conversions stay per-search rather
  than per-row (numpy is an optional accelerator, not a declared dependency).
- ``TestRowStemMemo`` — hybrid semantic retrieval derives a stored row's stem
  token set once per distinct text instead of once per row per query, and a row
  whose text changes is tokenized afresh rather than served from the memo.

Nothing here asserts a duration or a rate: the assertions are on SHAPE — an
invocation trace that does not grow when the input doubles, and equality of the
answer between the two rungs.
"""

from __future__ import annotations

import builtins
import hashlib
import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew import vector_memory as vm
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
        """get_lessons() is not called as an emptiness probe."""
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

    `_sqlite_vector_search` is the tier a default install runs (faiss-cpu is not
    a declared dependency). The numpy branch must be a drop-in: same ids, same
    order, same cosine values as the stdlib loop.
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
    """The sqlite episodic tier keeps its scoring columns resident.

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

    If `_build_episodic_scoring_set` returning None (population over
    `_EPISODIC_SCORING_MAX_BYTES`) is not memoized, every search full-scans the
    population trying to build, then full-scans again to answer per-call. The
    refusal is memoized under the same (dim, generation, data_version)
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


def _spread_embed(dim: int):
    """Deterministic embedding with real numeric spread, no model and no network.

    ``_fake_embed`` above cycles over seven values, which makes every pair of
    vectors nearly collinear — useless for showing that two dot-product
    implementations agree. This one is a linear congruential walk seeded by the
    text, so distinct texts point in unrelated directions.

    The seed comes from sha256, NOT from ``hash()``: str hashing is salted per
    process, so a ``hash()`` seed would give this fixture a different geometry on
    every run and any assertion about its cosines would be a coin toss.
    """

    def _embed(text: str) -> list[float]:
        state = int.from_bytes(hashlib.sha256(text.encode()).digest()[:4], "big") % 2147483647 or 1
        out: list[float] = []
        for _ in range(dim):
            state = (state * 48271) % 2147483647
            out.append(state / 2147483647.0 - 0.5)
        return out

    return _embed


#: A realistic embedding width, and one where the two rungs' summation orders
#: genuinely diverge: the numpy mat-vec dots in float32 (matching the stored
#: blob) while the stdlib loop accumulates in float64. Over this fixture the two
#: differ by ~6e-8 against a ~1e-6 worst-case margin to the nearest boundary of
#: the 4-decimal rounding both rungs apply, so the equality assertions below have
#: an order of magnitude of headroom rather than sitting on a knife edge.
_EPISODIC_DIM = 256


def _fixture_text(i: int) -> str:
    # The first 80 characters must differ per row: write_episodic prefix-dedups
    # on them, so a shared prefix would silently drop most of the fixture.
    return f"fragment {i:04d} - topic-{i % 7} notes about the gate and the rung"


def _sqlite_search_store(tmp_path: Path, n_entries: int, dim: int = _EPISODIC_DIM):
    """Store with *n_entries* embedded episodic rows and no FAISS index.

    ``_faiss_index`` is cleared so the stdlib rung is exercised whether or not
    faiss happens to be installed on the host.
    """
    store = VectorMemoryStore(db_path=tmp_path / "mem.db", embedding_dim=dim)
    store.init()
    store.embed_fn = _spread_embed(dim)
    for i in range(n_entries):
        assert store.write_episodic(_fixture_text(i)), f"fixture row {i} was rejected"
    store._faiss_index = None
    return store


def _query_near_rows(*row_indexes: int) -> list[float]:
    """A query vector deliberately close to the named fixture rows.

    Two unrelated pseudo-random vectors are near-orthogonal, so a query built
    from unrelated text scores every row at ~0 cosine and the relevance gate
    admits NOTHING — which makes any test downstream of that gate pass
    vacuously. Averaging the target rows' own embeddings puts them above the
    admission threshold and leaves the rest below it, so the gate is exercised
    in both directions without touching the threshold itself.
    """
    embed = _spread_embed(_EPISODIC_DIM)
    vectors = [embed(_fixture_text(i)) for i in row_indexes]
    return [sum(v[d] for v in vectors) / len(vectors) for d in range(_EPISODIC_DIM)]


class _CountingNumpy:
    """Delegates to numpy while tallying the two calls the shape tests pin."""

    def __init__(self, real) -> None:  # type: ignore[no-untyped-def]
        self._real = real
        self.asarray_calls = 0
        self.frombuffer_calls = 0

    def asarray(self, *a, **k):  # type: ignore[no-untyped-def]
        self.asarray_calls += 1
        return self._real.asarray(*a, **k)

    def frombuffer(self, *a, **k):  # type: ignore[no-untyped-def]
        self.frombuffer_calls += 1
        return self._real.frombuffer(*a, **k)

    def __getattr__(self, name):  # type: ignore[no-untyped-def]
        return getattr(self._real, name)


@pytest.mark.skipif(not _HAS_NUMPY, reason="numpy is an optional accelerator")
class TestSqliteVectorSearchNumpyRung:
    """The vectorized rung must be a pure speedup: same answer, hoisted setup.

    ``TestEpisodicSqliteCosineNumpy`` pins the same equality on a hand-inserted
    4-row fixture. This class approaches it from the other end: rows written
    through ``write_episodic`` with a real ``embed_fn``, at a width where the
    two rungs' summation orders actually diverge, and a query aimed close enough
    to specific rows to carry the relevance gate in both directions.
    """

    def test_numpy_and_stdlib_rungs_agree(self, tmp_path: Path) -> None:
        """Identical id order and cosines on the same rows.

        The two rungs differ in summation order and in dtype (numpy dots in
        float32, matching the stored blob; the stdlib loop accumulates in
        float64), so agreement here is what makes the change a speedup and not a
        retuning: the cosine it produces is also what ``_filter_by_relevance``
        compares against a fixed admission threshold.
        """
        store = _sqlite_search_store(tmp_path, 40)
        query = _spread_embed(_EPISODIC_DIM)("which rung scored the gate")

        with_numpy = store._sqlite_vector_search(query, "gate rung", 40, mmr=False)
        with patch.object(vm, "_HAS_NUMPY", False):
            with_stdlib = store._sqlite_vector_search(query, "gate rung", 40, mmr=False)

        assert [c["id"] for c in with_numpy] == [c["id"] for c in with_stdlib]
        assert with_numpy, "fixture produced no candidates"
        for a, b in zip(with_numpy, with_stdlib):
            assert abs(a["cosine_sim"] - b["cosine_sim"]) < 1e-6
            assert abs(a["score"] - b["score"]) < 1e-6

    def test_rungs_agree_through_mmr_and_relevance_gate(self, tmp_path: Path) -> None:
        """Equality survives the stages that consume the cosine downstream.

        ``relevance_filter=True`` compares the cosine against a fixed admission
        threshold, so a rung that shifted the value would drop or admit
        different rows here even where it left the ordering alone.
        """
        store = _sqlite_search_store(tmp_path, 40)
        query = _query_near_rows(3, 11)

        # Asked for the whole fixture and unreranked, so the length below is the
        # GATE's own answer rather than the limit's: a query that admitted
        # everything, or nothing, would let the parity assertions pass without
        # the threshold ever having decided anything.
        gated = store._sqlite_vector_search(
            query, "gate rung", 40, mmr=False, relevance_filter=True
        )
        assert 0 < len(gated) < 40, f"the relevance gate admitted {len(gated)} of 40 rows"

        with_numpy = store._sqlite_vector_search(query, "gate rung", 8, relevance_filter=True)
        with patch.object(vm, "_HAS_NUMPY", False):
            with_stdlib = store._sqlite_vector_search(query, "gate rung", 8, relevance_filter=True)

        assert with_numpy, "fixture produced no admitted candidates"
        assert [c["id"] for c in with_numpy] == [c["id"] for c in with_stdlib]
        for a, b in zip(with_numpy, with_stdlib):
            assert abs(a["cosine_sim"] - b["cosine_sim"]) < 1e-6
            assert abs(a["score"] - b["score"]) < 1e-6

    def test_numpy_conversions_do_not_grow_with_the_population(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Doubling the rows leaves the numpy conversion trace IDENTICAL.

        Both conversions are per-SEARCH work, not per-row work: the query vector
        is the same for every row, and the stored side is one buffer read over
        the joined blobs. Converting either inside the row loop is invisible in
        the answer, which is why this is asserted as a call trace rather than as
        a duration — a timed ratio false-reds on a shared runner, and an
        absolute count would pin the number of numpy calls the implementation
        happens to make rather than the property that matters.
        """
        counts = []
        for n_rows in (20, 40):
            store = _sqlite_search_store(tmp_path / f"n{n_rows}", n_rows)
            counting = _CountingNumpy(vm.np)
            monkeypatch.setattr(vm, "np", counting)
            try:
                results = store._sqlite_vector_search(
                    _spread_embed(_EPISODIC_DIM)("gate"), "gate", n_rows, mmr=False
                )
            finally:
                monkeypatch.undo()
            assert len(results) == n_rows
            assert counting.asarray_calls, "the numpy rung was never entered"
            counts.append((counting.asarray_calls, counting.frombuffer_calls))

        small, large = counts
        assert small == large, (
            f"the numpy conversion trace grew with the population: {small} -> {large}; "
            "both the query vector and the stored block are converted once per "
            "search, not once per row"
        )

    def test_stdlib_rung_stays_reachable_without_numpy(self, tmp_path: Path) -> None:
        """numpy is not a declared dependency, so the stdlib rung must work."""
        store = _sqlite_search_store(tmp_path, 12)
        query = _spread_embed(_EPISODIC_DIM)("gate")
        with patch.object(vm, "_HAS_NUMPY", False):
            results = store._sqlite_vector_search(query, "gate", 5, mmr=False)
        assert len(results) == 5
        assert all(-1.0 <= c["cosine_sim"] <= 1.0 for c in results)

    def test_mismatched_dimension_rows_are_skipped_on_both_rungs(self, tmp_path: Path) -> None:
        """A row from a previous embedding space is incomparable, not truncated."""
        store = _sqlite_search_store(tmp_path, 6)
        victim = store.db.execute(
            "SELECT id FROM episodic_memories WHERE is_deleted = 0 LIMIT 1"
        ).fetchone()["id"]
        # Half-width vector: a row written under a narrower embedding model.
        store.db.execute(
            "UPDATE episodic_memories SET embedding = ? WHERE id = ?",
            (b"\x00\x00\x80?" * (_EPISODIC_DIM // 2), victim),
        )
        store.db.commit()
        query = _spread_embed(_EPISODIC_DIM)("gate")

        numpy_hits = store._sqlite_vector_search(query, "gate", 10, mmr=False)
        with patch.object(vm, "_HAS_NUMPY", False):
            stdlib_hits = store._sqlite_vector_search(query, "gate", 10, mmr=False)
        numpy_ids = {c["id"] for c in numpy_hits}
        stdlib_ids = {c["id"] for c in stdlib_hits}
        assert victim not in numpy_ids
        assert numpy_ids == stdlib_ids


@pytest.fixture
def clean_stem_memos():
    """Isolate the row-side memo, which is module-level and shared per process.

    Deliberately leaves ``_stem_one`` alone: nothing here asserts its counters, and
    it is the large per-word Snowball memo every other memory test in the worker
    shares, so clearing it would discard a warm cache for no assertion.
    """
    vm._row_stem_tokens.cache_clear()
    yield
    vm._row_stem_tokens.cache_clear()


def _semantic_store(tmp_path: Path, n_rows: int) -> VectorMemoryStore:
    """Keyword-only semantic store: no ``embed_fn``, so scoring is stem overlap."""
    store = VectorMemoryStore(db_path=tmp_path / "mem.db")
    store.init()
    for i in range(n_rows):
        assert (
            store.set_semantic(
                f"pref.topic_{i}",
                f"the runner rebuilds gateways and caches result number {i}",
                confidence=1.0,
                source="user_explicit",
            )
            is None
        )
    return store


@pytest.mark.usefixtures("clean_stem_memos")
class TestRowStemMemo:
    """Row-side tokenization is derived per distinct text, not per query."""

    def test_memo_reproduces_the_unmemoized_token_set(self) -> None:
        """The memo is an idiom change, not a tokenization change."""
        for text in ("running gateways rebuild caches", "pref topic 3", "", "MiXeD Case"):
            expected = vm._stem_words(set(re.findall(r"\w+", text)))
            assert vm._row_stem_tokens(text) == expected

    def test_a_scan_that_fits_the_cache_uses_the_memo(self) -> None:
        """Under the bound, the memoized form is what a scan gets."""
        assert vm._row_stem_tokens_for_scan(vm._ROW_STEM_CACHE_SIZE) is vm._row_stem_tokens

    def test_a_scan_wider_than_the_cache_bypasses_the_memo(self) -> None:
        """Past the bound the memo cannot hit, so the scan must not pay for it.

        A repeated full-table scan is LRU's worst case: once the pass touches more
        entries than the cache holds, every lookup evicts the entry the next one
        needs and the hit rate is exactly zero, leaving only the wrapper cost and
        the retained frozensets. Selecting the uncached form is what keeps a store
        that outgrows the bound from paying for a cache it can never read.
        """
        wide = vm._ROW_STEM_CACHE_SIZE + 1
        assert vm._row_stem_tokens_for_scan(wide) is vm._row_stem_tokens_uncached

    def test_both_forms_agree(self) -> None:
        """Bypassing the memo must not change the tokens, only who derives them."""
        for text in ("running gateways rebuild caches", "pref topic 3", "", "MiXeD Case"):
            assert vm._row_stem_tokens_uncached(text) == vm._row_stem_tokens(text)

    def test_a_wide_scan_leaves_the_memo_untouched(self, tmp_path: Path) -> None:
        """The bypass is observable end to end, not just at the selector.

        Patching the bound below the row count is what makes this cheap: seeding
        4,097 real rows to cross the shipped bound would dominate the file's
        runtime, and the property under test is the width comparison, not the
        number it compares against.
        """
        store = _semantic_store(tmp_path, 30)
        with patch.object(vm, "_ROW_STEM_CACHE_SIZE", 4):
            store.get_semantic_context("rebuild the gateway", cap=4000)
        assert vm._row_stem_tokens.cache_info().misses == 0
        assert vm._row_stem_tokens.cache_info().hits == 0

    def test_row_side_trace_is_identical_when_the_query_count_doubles(self, tmp_path: Path) -> None:
        """Two more queries over the same rows cost ZERO new row tokenizations.

        This is the shape assertion for the memo: the row side depends only on
        the row's text, so doubling the queries must leave its miss count
        untouched. ``_stem_one`` may still miss on the new query's own words —
        the query side is deliberately NOT memoized, or a per-message cache
        would evict the bounded row population it exists to keep.
        """
        store = _semantic_store(tmp_path, 30)

        for query in ("rebuild the gateway", "cache the runner result"):
            store.get_semantic_context(query, cap=4000)
        after_first_pass = vm._row_stem_tokens.cache_info().misses
        assert after_first_pass > 0, "expected the first pass to populate the memo"

        for query in ("gateway runner rebuild", "which result was cached"):
            store.get_semantic_context(query, cap=4000)
        assert vm._row_stem_tokens.cache_info().misses == after_first_pass

    def test_first_pass_tokenizes_each_row_text_once(self, tmp_path: Path) -> None:
        """Misses scale with distinct row texts, not with rows times queries."""
        n_rows = 30
        store = _semantic_store(tmp_path, n_rows)
        # Two entries per row — one for the key, one for the value — derived from
        # the row count rather than restated, so the fixture and the expectation
        # cannot drift apart.
        expected_misses = 2 * n_rows
        store.get_semantic_context("rebuild the gateway", cap=4000)
        assert vm._row_stem_tokens.cache_info().misses == expected_misses
        store.get_semantic_context("cache the runner result", cap=4000)
        assert vm._row_stem_tokens.cache_info().misses == expected_misses

    def test_changed_row_text_is_tokenized_afresh(self, tmp_path: Path) -> None:
        """A stale memo serving the old token set would be a CORRECTNESS bug.

        The memo is keyed on the row's own text, so an updated value hashes to a
        different entry. Asserted through the retrieval answer rather than the
        cache, because that is where a stale token set would surface: the row
        would keep matching a word absent from its current text.
        """
        store = VectorMemoryStore(db_path=tmp_path / "mem.db")
        store.init()
        assert (
            store.set_semantic("pref.editor", "helix", confidence=1.0, source="user_explicit")
            is None
        )
        assert "helix" in store.get_semantic_context("helix", cap=4000)

        assert (
            store.set_semantic("pref.editor", "kakoune", confidence=1.0, source="user_explicit")
            is None
        )
        after = store.get_semantic_context("helix", cap=4000)
        assert "helix" not in after
        assert "kakoune" not in after, "the row still matched a word it no longer holds"
        assert "kakoune" in store.get_semantic_context("kakoune", cap=4000)

    def test_stemming_still_matches_an_inflected_query(self, tmp_path: Path) -> None:
        """The memo keeps the stem expansion that makes recall work."""
        store = VectorMemoryStore(db_path=tmp_path / "mem.db")
        store.init()
        assert (
            store.set_semantic(
                "pref.style",
                "the runner rebuilds gateways",
                confidence=1.0,
                source="user_explicit",
            )
            is None
        )
        # "rebuilding"/"gateway" only match through the Snowball expansion.
        assert "rebuilds" in store.get_semantic_context("rebuilding gateway", cap=4000)

    def test_memo_is_bounded_by_a_named_constant(self) -> None:
        """An unbounded memo keyed on user memory content is not acceptable."""
        assert vm._row_stem_tokens.cache_info().maxsize == vm._ROW_STEM_CACHE_SIZE
        assert isinstance(vm._ROW_STEM_CACHE_SIZE, int)
        assert vm._ROW_STEM_CACHE_SIZE > 0
