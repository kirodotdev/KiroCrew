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
