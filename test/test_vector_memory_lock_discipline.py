"""Tests for concurrent use of the vector store: ``_db_lock`` discipline and thread safety.

They pin writer-against-reader stress runs (``_join_stress_threads`` tells a deadlock from a
slow runner) and the per-thread stemmer, write paths that hold the lock under a proxy that
flags DML and episodic fetches issued without it, the stale-episodic retirement a semantic
write runs, the locked fetch helpers, the locked ``last_accessed_at`` update with
``busy_timeout``, and ``TestDbLockGuard``, an AST guard over ``vector_memory.py`` and its
runtime modules. The rule that async code offloads lock-taking store calls is pinned in
``test_vector_memory_event_loop_offload.py``.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from kiro_crew.vector_memory import (
    _HAS_FAISS,
    _HAS_NUMPY,
    _MAX_EPISODIC_RETIRED_PER_WRITE,
    VectorMemoryStore,
    _stem_words,
    _tokenize,
)

# Same xdist group as ``test_vector_memory.py`` and the topic files its docstring names.
# ``TestDbLockGuard`` reads vector_memory.py and the ``vector_memory_runtime`` modules; the
# two stress classes carry their own ``vector_memory_concurrency`` group, which takes
# precedence over this module mark.
pytestmark = pytest.mark.xdist_group(name="tree_scan_test_vector_memory")


def _join_stress_threads(
    threads: list[threading.Thread],
    progress: dict[int, int],
    label: str,
    *,
    window: float = 30.0,
    stall_windows: int = 2,
) -> None:
    """Join stress threads, distinguishing a real deadlock from a slow runner.

    A plain ``join(timeout=60)`` cannot tell "the lock is stuck" from "this
    Windows runner is overloaded" — it fails both the same way, so a genuine
    deadlock and mere slowness produce the same red. Per AGENTS.md a timing
    test must assert the *shape* (does work advance?), not a wall-clock
    duration.

    Each worker records its completed-iteration count in ``progress`` (keyed by
    ``threading.get_ident()``). We observe in ``window``-second slices: a thread
    is only declared deadlocked if it is still alive AND the workers made zero
    forward progress across ``stall_windows`` consecutive windows. A stalled
    lock never advances; a slow-but-live worker keeps incrementing its counter
    and is simply waited on. Slowness alone is never a failure.

    Each observation window costs ~``window`` seconds *in total*, not per
    thread: the per-thread join timeout is divided by the live-thread count and
    the window is capped by a monotonic deadline. Joining N threads for the full
    ``window`` each would make one window take ``window * N`` — with 8 stalled
    workers that is 240s, past the pytest ``--timeout`` kill, so a real deadlock
    would hang to the harness kill instead of failing cleanly. Bounding the
    window keeps the ``stall_windows * window`` no-progress budget (~60s at the
    defaults) safely under that timeout.
    """
    stalled_windows = 0
    last_total = -1
    while any(t.is_alive() for t in threads):
        # flake-ok: pre-existing monotonic deadline bounding each join slice, moved verbatim
        deadline = time.monotonic() + window
        for t in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            live = sum(1 for x in threads if x.is_alive()) or 1
            t.join(timeout=min(remaining, window / live))
        total = sum(progress.values())
        if any(t.is_alive() for t in threads):
            if total == last_total:
                stalled_windows += 1
                if stalled_windows >= stall_windows:
                    raise AssertionError(
                        f"{label}: no progress across {stall_windows} x "
                        f"{window:g}s windows ({total} iterations done) — "
                        f"genuine deadlock, not slow runner"
                    )
            else:
                stalled_windows = 0
            last_total = total


@pytest.mark.xdist_group("vector_memory_concurrency")
class TestVectorStoreConcurrency:
    """Writes are offloaded to worker threads (consolidation, dashboard) while
    reads (search_episodic via context assembly) run on the event loop thread.

    The shared sqlite connection and the non-thread-safe FAISS index /
    _faiss_id_map must be serialized by _db_lock. Without it, a concurrent
    write_episodic (faiss.add + id_map.append) racing a search_episodic can
    IndexError on _faiss_id_map[idx] (add-before-append window) or corrupt the
    C++ index. Regression guard for the loop-offload concurrency finding.
    """

    def test_concurrent_write_and_search_no_crash(self, tmp_path, opened) -> None:
        if not (_HAS_FAISS and _HAS_NUMPY):
            pytest.skip("FAISS/numpy not available on this platform")

        dim = 16

        def _fake_embed(text: str):
            # Deterministic pseudo-embedding derived from the text so FAISS has
            # real vectors to add/search without a network call.
            seed = sum(ord(c) for c in text)
            return [float((seed + i) % 7) + 0.1 for i in range(dim)]

        store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db", embedding_dim=dim))
        store.init()
        store.embed_fn = _fake_embed
        store.build_faiss_index()

        errors: list[BaseException] = []

        def _writer(n: int) -> None:
            try:
                for i in range(n):
                    store.write_episodic(f"episodic memory number {i} about topic alpha beta")
            except BaseException as exc:  # noqa: BLE001 - capture any thread crash
                errors.append(exc)

        def _searcher(n: int) -> None:
            try:
                for _ in range(n):
                    store.search_episodic(query_text="topic alpha", limit=5)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=_writer, args=(40,)),
            threading.Thread(target=_writer, args=(40,)),
            threading.Thread(target=_searcher, args=(60,)),
            threading.Thread(target=_searcher, args=(60,)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            # flake-ok: pre-existing thread join with no timeout, moved verbatim
            t.join()

        assert not errors, f"concurrent write/search raised: {errors!r}"

    def test_concurrent_write_lesson_and_get_lessons_no_crash(self, tmp_path, opened) -> None:
        """write_lesson is now offloaded to worker threads (consolidation, task
        runner) concurrent with loop-thread readers (get_lessons, get_lessons_context).

        Regression guard for the _save_lessons offload. write_lesson touches raw
        self.db without _db_lock on some paths (get_lessons SELECT, delete_semantic
        tombstone, embedding backfill UPDATEs). Races on the shared connection
        surface as arbitrary transient exceptions (OperationalError "no more rows
        available", corrupt rows exploding as TypeError in json.loads) whose type
        varies by platform and sqlite driver (stdlib vs pysqlite3), so per-call
        exceptions are tolerated — production callers already swallow and log
        them (a lost lesson write is retried on the next consolidation). The
        real guard: the stress must not deadlock, segfault, or corrupt the
        store — verified by the post-stress functional write/read cycle.
        """
        dim = 16

        def _fake_embed(text: str):
            seed = sum(ord(c) for c in text)
            return [float((seed + i) % 7) + 0.1 for i in range(dim)]

        store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db", embedding_dim=dim))
        store.init()
        store.embed_fn = _fake_embed

        transient: list[BaseException] = []
        progress: dict[int, int] = {}

        def _lesson_writer(n: int) -> None:
            tid = threading.get_ident()
            progress[tid] = 0
            for i in range(n):
                try:
                    store.write_lesson(
                        rule=f"always verify step {i} before proceeding",
                        category="tool",
                        source="consolidation",
                    )
                except Exception as exc:  # noqa: BLE001 — see docstring
                    transient.append(exc)
                progress[tid] += 1

        def _lesson_reader(n: int) -> None:
            tid = threading.get_ident()
            progress[tid] = 0
            for _ in range(n):
                try:
                    store.get_lessons()
                    store.get_lessons_context()
                except Exception as exc:  # noqa: BLE001 — see docstring
                    transient.append(exc)
                progress[tid] += 1

        threads = [
            threading.Thread(target=_lesson_writer, args=(20,)),
            threading.Thread(target=_lesson_writer, args=(20,)),
            threading.Thread(target=_lesson_reader, args=(40,)),
            threading.Thread(target=_lesson_reader, args=(40,)),
        ]
        for t in threads:
            t.start()
        _join_stress_threads(threads, progress, "lesson write/read stress")

        # Store must remain fully functional after the stress: a fresh
        # write/read cycle succeeds and returns coherent lesson rows.
        # (transient per-call errors during the stress are acceptable; a
        # corrupted store here is not.)
        assert store.write_lesson(
            rule="post-stress smoke lesson about zeta functionality",
            category="tool",
            source="consolidation",
        ), f"store broken after stress (transient errors seen: {transient!r})"
        lessons = store.get_lessons()
        assert any("zeta functionality" in str(ls.get("value_json", "")) for ls in lessons)

    def test_concurrent_semantic_write_and_context_no_errors(self, tmp_path, opened) -> None:
        """get_semantic_context runs on executor threads (subagent context builds
        via run_in_embed_pool) concurrent with set_semantic writers on worker
        threads. Its SELECTs must not hit the shared connection WITHOUT _db_lock,
        racing writers' implicit transactions and the per-connection statement
        cache — observed in production as sqlite3.InterfaceError ("bad parameter
        or other API misuse") from get_semantic_context, which propagates
        unguarded through memory.get_context -> context.build_session_context
        and kills the whole subagent run.

        Unlike the lesson stress above, readers here must raise NOTHING: with
        the fetches locked, every db access on this path is serialized, and the
        production caller has no try/except to absorb a transient.
        """
        store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db"))
        store.init()
        # Seed rows so the reader's scoring loop has real work between fetches.
        for i in range(20):
            store.set_semantic(f"project.seed.k{i:02d}", f"alpha beta value {i}", 1.0, "tool")

        start_barrier = threading.Barrier(4)
        errors: list[BaseException] = []
        progress: dict[int, int] = {}

        def _writer(n: int, tag: str) -> None:
            tid = threading.get_ident()
            progress[tid] = 0
            # flake-ok: pre-existing barrier wait with no timeout, moved verbatim
            start_barrier.wait()
            try:
                for i in range(n):
                    store.set_semantic(f"project.stress.{tag}{i:03d}", f"gamma {i}", 1.0, "tool")
                    progress[tid] += 1
            except BaseException as exc:  # noqa: BLE001 - capture any thread crash
                errors.append(exc)

        def _context_reader(n: int) -> None:
            tid = threading.get_ident()
            progress[tid] = 0
            # flake-ok: pre-existing barrier wait with no timeout, moved verbatim
            start_barrier.wait()
            try:
                for i in range(n):
                    # Alternate branches: query-scored path and recency path.
                    if i % 2:
                        store.get_semantic_context(query_text="alpha beta gamma")
                    else:
                        store.get_semantic_context()
                    progress[tid] += 1
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=_writer, args=(60, "a")),
            threading.Thread(target=_writer, args=(60, "b")),
            threading.Thread(target=_context_reader, args=(80,)),
            threading.Thread(target=_context_reader, args=(80,)),
        ]
        for t in threads:
            t.start()
        _join_stress_threads(threads, progress, "semantic write/context stress")
        assert not errors, f"concurrent set_semantic/get_semantic_context raised: {errors!r}"

        # Coherence: a post-stress read returns the seeded entries intact.
        ctx = store.get_semantic_context(query_text="alpha beta")
        assert "project.seed.k00" in ctx

    def test_concurrent_lesson_write_and_get_lessons_context_no_reader_errors(
        self, tmp_path, opened
    ) -> None:
        """get_lessons_context feeds the same unguarded context-injection path
        (context.py calls it with no try/except), so with get_lessons' fetch
        now serialized the READERS must not raise. Writer-side transients are
        tolerated as in the lesson stress above — write_lesson still has
        unlocked segments outside the fetch (embed, dedup logic).
        """
        store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db"))
        store.init()
        for i in range(10):
            store.write_lesson(
                rule=f"seed lesson number {i} about distinct topic {i}",
                category="tool",
                source="consolidation",
            )

        start_barrier = threading.Barrier(4)
        reader_errors: list[BaseException] = []
        progress: dict[int, int] = {}

        def _lesson_writer(n: int, tag: str) -> None:
            tid = threading.get_ident()
            progress[tid] = 0
            # flake-ok: pre-existing barrier wait with no timeout, moved verbatim
            start_barrier.wait()
            for i in range(n):
                try:
                    store.write_lesson(
                        rule=f"stress lesson {tag}{i:03d} about unique subject {tag}{i}",
                        category="tool",
                        source="consolidation",
                    )
                except Exception:  # noqa: BLE001 - writer transients tolerated
                    pass
                progress[tid] += 1

        def _lesson_reader(n: int) -> None:
            tid = threading.get_ident()
            progress[tid] = 0
            # flake-ok: pre-existing barrier wait with no timeout, moved verbatim
            start_barrier.wait()
            try:
                for _ in range(n):
                    store.get_lessons_context()
                    progress[tid] += 1
            except BaseException as exc:  # noqa: BLE001
                reader_errors.append(exc)

        threads = [
            threading.Thread(target=_lesson_writer, args=(30, "a")),
            threading.Thread(target=_lesson_writer, args=(30, "b")),
            threading.Thread(target=_lesson_reader, args=(80,)),
            threading.Thread(target=_lesson_reader, args=(80,)),
        ]
        for t in threads:
            t.start()
        _join_stress_threads(threads, progress, "lesson write/context stress")
        assert not reader_errors, f"get_lessons_context raised: {reader_errors!r}"

    def test_stem_words_thread_safe(self) -> None:
        """_stem_words must not share ONE module-level snowballstemmer instance.
        The pure-Python stemmer keeps the word under stem as mutable instance
        state (set_current -> _stem -> get_current), so concurrent
        get_semantic_context calls (parallel subagent context builds)
        interleaved that cursor and raised IndexError("string index out of
        range") — or silently produced wrong stems. Now one instance per
        thread: hammering _stem_words from many threads must neither raise nor
        diverge from the single-threaded result.
        """
        vocab = [f"word{i} running jumped happily nationalization {i}" for i in range(50)]
        word_sets = [set(v.split()) for v in vocab]
        expected = [_stem_words(ws) for ws in word_sets]

        start_barrier = threading.Barrier(8)
        errors: list[BaseException] = []
        progress: dict[int, int] = {}

        def _stemmer_worker() -> None:
            tid = threading.get_ident()
            progress[tid] = 0
            # flake-ok: pre-existing barrier wait with no timeout, moved verbatim
            start_barrier.wait()
            try:
                for _ in range(40):
                    for ws, exp in zip(word_sets, expected):
                        assert _stem_words(ws) == exp
                    progress[tid] += 1
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=_stemmer_worker) for _ in range(8)]
        for t in threads:
            t.start()
        _join_stress_threads(threads, progress, "stemmer stress")
        assert not errors, f"concurrent _stem_words raised/diverged: {errors!r}"


class TestSearchLastAccessedLocking:
    """Regression guard for the unlocked last_accessed_at UPDATE in the FAISS
    search path. The metadata UPDATE must run under _db_lock (the same lock as
    the write path) inside a single committed transaction so it neither races
    concurrent writers nor is lost/clobbered, and does not raise 'database is
    locked'. busy_timeout is configured at connection init.
    """

    def _fake_embed(self, dim: int):
        def _embed(text: str):
            seed = sum(ord(c) for c in text)
            return [float((seed + i) % 7) + 0.1 for i in range(dim)]

        return _embed

    def test_busy_timeout_configured(self, tmp_path: Path, opened) -> None:
        store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db"))
        store.init()
        # busy_timeout is set in milliseconds at connection init so contention is
        # waited out rather than immediately erroring.
        timeout = store.db.execute("PRAGMA busy_timeout").fetchone()[0]
        assert timeout >= 1000

    def test_search_updates_last_accessed_under_lock(self, tmp_path: Path, opened) -> None:
        if not (_HAS_FAISS and _HAS_NUMPY):
            pytest.skip("FAISS/numpy not available on this platform")

        dim = 16
        store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db", embedding_dim=dim))
        store.init()
        store.embed_fn = self._fake_embed(dim)
        store.build_faiss_index()
        store.write_episodic("User decided to use PostgreSQL for the database layer")

        # The FAISS search path must acquire _db_lock while writing last_accessed_at.
        # Wrap the real RLock so we can assert it was actually held during the search.
        real_lock = store._db_lock
        acquisitions = {"count": 0}

        class _CountingLock:
            def __enter__(self):
                acquisitions["count"] += 1
                return real_lock.__enter__()

            def __exit__(self, *exc):
                return real_lock.__exit__(*exc)

        store._db_lock = _CountingLock()  # type: ignore[assignment]
        embed = self._fake_embed(dim)
        results = store.search_episodic(
            query_embedding=embed("PostgreSQL database"),
            query_text="PostgreSQL database",
            limit=5,
        )
        store._db_lock = real_lock  # type: ignore[assignment]

        assert results, "expected at least one episodic hit"
        # The search candidate section AND the last_accessed_at UPDATE both take
        # the lock → at least two acquisitions (>=1 proves the UPDATE is locked).
        assert acquisitions["count"] >= 2
        # last_accessed_at was actually persisted.
        row = store.db.execute(
            "SELECT last_accessed_at FROM episodic_memories WHERE id = ?",
            (results[0]["id"],),
        ).fetchone()
        assert row["last_accessed_at"] is not None

    def test_concurrent_search_updates_no_lock_error(self, tmp_path: Path, opened) -> None:
        if not (_HAS_FAISS and _HAS_NUMPY):
            pytest.skip("FAISS/numpy not available on this platform")

        dim = 16
        store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db", embedding_dim=dim))
        store.init()
        store.embed_fn = self._fake_embed(dim)
        store.build_faiss_index()
        for i in range(10):
            store.write_episodic(f"episodic memory number {i} about topic alpha beta")

        errors: list[BaseException] = []
        embed = self._fake_embed(dim)
        q_vec = embed("topic alpha")

        def _searcher(n: int) -> None:
            try:
                for _ in range(n):
                    store.search_episodic(query_embedding=q_vec, query_text="topic alpha", limit=5)
            except BaseException as exc:  # noqa: BLE001 - capture any thread crash
                errors.append(exc)

        threads = [threading.Thread(target=_searcher, args=(50,)) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            # flake-ok: pre-existing thread join with no timeout, moved verbatim
            t.join()

        # The locked, transactional UPDATE must not surface 'database is locked'
        # or interleave into a crash under concurrent search load.
        assert not errors, f"concurrent search-update raised: {errors!r}"


class _TrackingLock:
    """RLock wrapper that records, per thread, whether the lock is held."""

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self._local = threading.local()

    @property
    def held(self) -> bool:
        return getattr(self._local, "depth", 0) > 0

    def __enter__(self) -> "_TrackingLock":
        self._inner.__enter__()  # type: ignore[attr-defined]
        self._local.depth = getattr(self._local, "depth", 0) + 1
        return self

    def __exit__(self, *exc: object) -> object:
        self._local.depth = getattr(self._local, "depth", 1) - 1
        return self._inner.__exit__(*exc)  # type: ignore[attr-defined]


class _AuditingConnection:
    """sqlite connection proxy that flags statements issued without ``_db_lock``.

    Python's sqlite3 emits an implicit ``BEGIN`` before INSERT/UPDATE/DELETE
    when it observes ``sqlite3_get_autocommit() == 1``. Two threads writing on
    the same connection can both pass that check and both issue ``BEGIN``, and
    the loser raises ``OperationalError: cannot start a transaction within a
    transaction``. Holding ``_db_lock`` across every DML statement is what
    prevents it, so this proxy audits that discipline directly.

    Reads are audited too, but only the two episodic-search fetches, which the
    context-assembly path runs concurrently with memory writes. sqlite3 caches
    prepared statements per connection, so a SELECT that overlaps another
    statement can have its row iteration corrupted — that surfaced on Windows
    CI as a NULL ``embedding`` from a query filtering ``embedding IS NOT NULL``,
    raising ``TypeError`` on ``len()``. The other read methods are deliberately
    NOT audited: they run unlocked by design and widening the audit to them
    would assert an invariant this change does not establish.
    """

    _DML = ("INSERT", "UPDATE", "DELETE", "REPLACE", "BEGIN")
    _GUARDED_READS = ("embedding IS NOT NULL", "text LIKE ?")

    def __init__(self, inner: object, lock: _TrackingLock, violations: list[str]) -> None:
        self._inner = inner
        self._lock = lock
        self._violations = violations

    def __enter__(self):
        self._inner.__enter__()
        return self

    def __exit__(self, *exc):
        return self._inner.__exit__(*exc)

    def _must_be_locked(self, sql: str) -> bool:
        if sql.lstrip().upper().startswith(self._DML):
            return True
        return any(frag in sql for frag in self._GUARDED_READS)

    def execute(self, sql: str, *args: object, **kwargs: object) -> object:
        if self._must_be_locked(sql) and not self._lock.held:
            self._violations.append(sql.strip()[:80])
        return self._inner.execute(sql, *args, **kwargs)  # type: ignore[attr-defined]

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


class TestSharedConnectionLockDiscipline:
    """Every DML on the shared sqlite connection must hold ``_db_lock``."""

    @staticmethod
    def _fake_embed(dim: int):
        def _embed(text: str) -> list[float]:
            words = _tokenize(text)
            vec = [0.0] * dim
            for w in words:
                vec[hash(w) % dim] += 1.0
            return vec or [1.0] + [0.0] * (dim - 1)

        return _embed

    def _instrument(self, store: VectorMemoryStore) -> list[str]:
        violations: list[str] = []
        lock = _TrackingLock(store._db_lock)
        store._db_lock = lock  # type: ignore[assignment]
        store._db = _AuditingConnection(store._db, lock, violations)  # type: ignore[assignment]
        return violations

    def test_write_paths_hold_db_lock(self, tmp_path: Path, opened) -> None:
        dim = 16
        store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db", embedding_dim=dim))
        store.init()
        embed = self._fake_embed(dim)
        store.embed_fn = embed
        violations = self._instrument(store)

        # Episodic writes + the two search fallbacks the context-assembly path
        # uses when faiss is unavailable: the vector scan (which also updates
        # last_accessed_at) and the LIKE fallback. Both fetches ran unlocked and
        # raced consolidation.
        store._faiss_index = None
        for i in range(3):
            text = f"the findings doc for topic alpha number {i} was written to disk"
            assert store.write_episodic(text, embedding=embed(text)) is True
        assert store.search_episodic(query_embedding=embed("findings doc"), limit=3)
        assert store.search_episodic(query_text="findings doc", limit=3)

        # Semantic write, then an overwrite so _retire_stale_episodic runs.
        assert (
            store.set_semantic("project.notes.findings_doc", "v1 draft", 0.9, "user_explicit")
            is None
        )
        assert (
            store.set_semantic("project.notes.findings_doc", "v2 final", 0.9, "user_explicit")
            is None
        )

        # Remaining writers: lessons (incl. embedding backfill), deletes, rotation.
        assert store.write_lesson("prefer explicit transactions over implicit ones").wrote is True
        store.delete_semantic("project.notes.findings_doc", "user_explicit")
        rows = store.get_episodic_list(limit=1)
        if rows:
            store.delete_episodic(rows[0]["id"])
        store.rotate_events(max_rows=1)

        assert violations == [], f"statement issued without _db_lock held: {violations}"

    def test_concurrent_search_and_semantic_write(self, tmp_path: Path, opened) -> None:
        """Consolidation-style writes must survive concurrent context assembly.

        Reproduces the shape of the observed failure: several context-assembly
        threads run the SQLite search fallback (which writes last_accessed_at)
        while a consolidation thread overwrites semantic keys (which retires
        stale episodic rows).
        """
        dim = 16
        store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db", embedding_dim=dim))
        store.init()
        embed = self._fake_embed(dim)
        store.embed_fn = embed
        store._faiss_index = None  # force the _sqlite_vector_search fallback
        for i in range(12):
            text = f"episodic entry {i} about the findings doc and topic alpha beta"
            store.write_episodic(text, embedding=embed(text))

        errors: list[BaseException] = []

        def _searcher() -> None:
            try:
                for _ in range(25):
                    store.search_episodic(query_embedding=embed("findings doc"), limit=5)
            except BaseException as exc:  # noqa: BLE001 - capture any thread crash
                errors.append(exc)

        def _writer() -> None:
            try:
                for i in range(25):
                    store.set_semantic(
                        "project.notes.findings_doc", f"revision {i}", 0.9, "consolidation"
                    )
            except BaseException as exc:  # noqa: BLE001 - capture any thread crash
                errors.append(exc)

        threads = [threading.Thread(target=_searcher) for _ in range(3)]
        threads.append(threading.Thread(target=_writer))
        for t in threads:
            t.start()
        for t in threads:
            # flake-ok: pre-existing thread join with no timeout, moved verbatim
            t.join()

        assert not errors, f"concurrent search/write raised: {errors!r}"
        entry = store.get_semantic("project.notes.findings_doc")
        assert entry is not None

    def test_retire_failure_keeps_semantic_write(self, tmp_path: Path, opened) -> None:
        """A retirement failure must not discard the committed semantic write.

        ``_write_semantic`` commits the row before retiring stale episodic
        entries, so an exception raised in step 9 propagated out of
        ``set_semantic`` and aborted the caller's whole batch — history
        consolidation lost every remaining semantic and episodic item.
        """
        store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db"))
        store.init()
        assert store.set_semantic("pref.editor", "vim", 0.9, "consolidation") is None

        def _boom(key: str, old_value: str, **kw: object) -> None:
            raise RuntimeError("cannot start a transaction within a transaction")

        store._retire_stale_episodic = _boom  # type: ignore[assignment]
        assert store.set_semantic("pref.editor", "emacs", 0.9, "user_explicit") is None
        entry = store.get_semantic("pref.editor")
        assert entry is not None
        assert entry["value_json"] == '"emacs"'

    def test_retire_search_skips_mmr_rerank(self, tmp_path: Path, opened) -> None:
        """``_retire_stale_episodic``'s internal lookup passes ``mmr=False``.

        The caller applies its own ``cosine_sim > 0.7`` threshold, so diversity
        reranking buys nothing there, and the default MMR rerank cost ~71ms per
        superseding semantic write at 1,000 pooled candidates.
        """
        dim = 16
        store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db", embedding_dim=dim))
        store.init()
        embed = self._fake_embed(dim)
        store.embed_fn = embed

        # Matches the retire query "editor: vim" exactly, so the vector path
        # clears its cosine threshold regardless of tokenizer details.
        text = "editor: vim"
        assert store.write_episodic(text, embedding=embed(text)) is True

        calls: list[dict] = []
        returned: list[list[dict]] = []
        real_search = store.search_episodic

        def _spy(*args: object, **kwargs: object) -> list[dict]:
            calls.append(dict(kwargs))
            out = real_search(*args, **kwargs)  # type: ignore[arg-type]
            returned.append(out)
            return out

        store.search_episodic = _spy  # type: ignore[assignment]
        assert store.set_semantic("pref.editor", "vim", 0.9, "consolidation") is None
        # The superseding write triggers _retire_stale_episodic on "vim".
        assert store.set_semantic("pref.editor", "emacs", 0.9, "consolidation") is None

        retire_calls = [c for c in calls if c.get("query_embedding") is not None]
        assert retire_calls, "superseding write never reached search_episodic"
        assert all(
            c.get("mmr") is False for c in retire_calls
        ), f"retirement search must skip the MMR rerank: {retire_calls}"

        # The VECTOR branch itself cleared the threshold: without this, the
        # is_deleted check below would be satisfied by the text-LIKE fallback
        # alone (the fixture text matches its first pattern verbatim) and a
        # broken vector path under mmr=False would keep the test green.
        assert any(
            r.get("cosine_sim", 0) > 0.7 for out in returned for r in out
        ), "vector retirement path returned no row above the cosine threshold"

        # The superseded episodic row is still soft-deleted without the rerank.
        row = store.db.execute(
            "SELECT is_deleted FROM episodic_memories WHERE text = ?", (text,)
        ).fetchone()
        assert row is not None
        assert row["is_deleted"] == 1

    def test_retire_recall_survives_without_the_mmr_pool(self, tmp_path: Path, opened) -> None:
        """The vector arm alone fills the per-write cap when MMR is off.

        ``mmr`` does not only pick a reranker — it sizes the candidate pool
        (``_MMR_MAX_POOL`` vs ``limit``), so the retirement lookup keeps a wide
        ``limit``: the caller's own ``cosine_sim > 0.7`` threshold, not the pool
        cut, decides which rows are candidates. The row texts avoid both
        text-LIKE fallback patterns ("editor: vim" / "editor vim"), so only the
        vector branch can retire them, and the write must retire exactly
        ``_MAX_EPISODIC_RETIRED_PER_WRITE`` of the fifteen conflicting rows: fewer
        means the vector branch under-delivered, more means the cap is not
        applied on V1.
        """
        dim = 64
        store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db", embedding_dim=dim))
        store.init()
        embed = self._fake_embed(dim)
        store.embed_fn = embed
        store._faiss_index = None  # deterministic: exercise the fallback tier

        fillers = [
            "alpha",
            "bravo",
            "charlie",
            "delta",
            "echo",
            "foxtrot",
            "golf",
            "hotel",
            "india",
            "juliet",
            "kilo",
            "lima",
            "mike",
            "november",
            "oscar",
        ]
        # Tokens {vim, editor, <filler>} give cosine ~0.82 against the retire
        # query "editor: vim" (tokens {editor, vim}) under the bag-of-words
        # fake embed — above the 0.7 threshold, below the 0.88 dedup gate.
        texts = [f"vim editor {w}" for w in fillers]
        for t in texts:
            assert store.write_episodic(t, embedding=embed(t)) is True

        assert store.set_semantic("pref.editor", "vim", 0.9, "consolidation") is None
        assert store.set_semantic("pref.editor", "emacs", 0.9, "consolidation") is None

        remaining = store.db.execute(
            "SELECT COUNT(*) FROM episodic_memories WHERE is_deleted = 0"
        ).fetchone()[0]
        assert (
            remaining == len(texts) - _MAX_EPISODIC_RETIRED_PER_WRITE
        ), f"{len(texts) - remaining} rows retired; the vector arm must spend exactly the cap"


class TestLockedFetchHelpers:
    """The locked fetch helpers — the single route for plain SELECTs."""

    def test_fetch_all_locked_returns_materialized_rows(self, tmp_path: Path, opened) -> None:
        store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db"))
        store.init()
        store.set_semantic("pref.editor", "vim", 0.9, "user_explicit")
        store.set_semantic("pref.shell", "zsh", 0.9, "user_explicit")
        rows = store._fetch_all_locked(
            "SELECT key FROM semantic_memory WHERE key LIKE ? AND is_deleted = 0 ORDER BY key",
            ("pref.%",),
        )
        # A materialized list (not a live cursor), safe to iterate unlocked.
        assert isinstance(rows, list)
        assert [r["key"] for r in rows] == ["pref.editor", "pref.shell"]

    def test_fetch_one_locked_hit_and_miss(self, tmp_path: Path, opened) -> None:
        store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db"))
        store.init()
        store.set_semantic("pref.editor", "vim", 0.9, "user_explicit")
        row = store._fetch_one_locked(
            "SELECT value_json FROM semantic_memory WHERE key = ?", ("pref.editor",)
        )
        assert row is not None and row["value_json"] == '"vim"'
        assert (
            store._fetch_one_locked(
                "SELECT value_json FROM semantic_memory WHERE key = ?", ("pref.nope",)
            )
            is None
        )

    def test_helpers_are_reentrant_under_held_lock(self, tmp_path: Path, opened) -> None:
        """_db_lock is an RLock: locked write sections may call readers that
        route through the helpers (e.g. search_episodic -> _get_episodic_batch)
        without deadlocking."""
        store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db"))
        store.init()
        store.set_semantic("pref.editor", "vim", 0.9, "user_explicit")
        with store._db_lock:
            rows = store._fetch_all_locked("SELECT key FROM semantic_memory")
        assert len(rows) == 1


class TestDbLockGuard:
    """AST guard: EVERY statement on the shared
    ``check_same_thread=False`` connection must be serialized on ``_db_lock``.

    Enforcing the contract by convention alone is error-prone. This test
    makes a raw unlocked ``self.db.execute(...)`` in vector_memory.py a CI
    failure instead of a code-review catch: new fetches must route through
    ``_fetch_all_locked``/``_fetch_one_locked`` (which lock internally) or sit
    inside an explicit ``with self._db_lock:`` read-modify-write section or
    ``_vector_commit``. The helper's own acquisition is checked too; removing it
    must fail this guard, not turn the helper's name into an exemption.
    """

    #: Methods allowed to touch the raw ``self._db`` attribute unlocked:
    #: they run before/after the store is shared across threads.
    _RAW_DB_ALLOWED = {"init", "_init_database", "close", "db"}

    #: The names a store is reached by: ``self`` inside ``VectorMemoryStore`` and
    #: ``store``, the first parameter of every ``vector_memory_runtime`` function.
    _RECEIVERS = ("self", "store")

    @classmethod
    def _is_self_attr(cls, node: object, attr: str) -> bool:
        import ast

        return (
            isinstance(node, ast.Attribute)
            and node.attr == attr
            and isinstance(node.value, ast.Name)
            and node.value.id in cls._RECEIVERS
        )

    @classmethod
    def _find_unserialized_statements(cls, tree) -> list[str]:
        """Return a violation string per sqlite statement not serialized on
        ``_db_lock``. Shared by the real guard and its self-test below."""
        import ast

        violations: list[str] = []
        guard = cls

        class Visitor(ast.NodeVisitor):
            def __init__(self) -> None:
                # The receivers whose ``_db_lock`` is held here: the lock on
                # ``store`` does not serialize a statement on ``self``.
                self.locked: tuple[str, ...] = ()
                self.exit_stacks: set[str] = set()
                self.func_stack: list[str] = []

            def visit_With(self, node: ast.With) -> None:
                saved_locked, saved_stacks = self.locked, self.exit_stacks.copy()
                for item in node.items:
                    expr = item.context_expr
                    if guard._is_self_attr(expr, "_db_lock"):
                        self.locked += (expr.value.id,)
                    elif isinstance(expr, ast.Call) and guard._is_self_attr(
                        expr.func, "_vector_commit"
                    ):
                        self.locked += (expr.func.value.id,)
                    if (
                        isinstance(expr, ast.Call)
                        and isinstance(expr.func, ast.Name)
                        and expr.func.id == "ExitStack"
                        and isinstance(item.optional_vars, ast.Name)
                    ):
                        self.exit_stacks.add(item.optional_vars.id)
                self.generic_visit(node)
                self.locked, self.exit_stacks = saved_locked, saved_stacks

            def visit_Expr(self, node: ast.Expr) -> None:
                self.generic_visit(node)
                call = node.value
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "enter_context"
                    and isinstance(call.func.value, ast.Name)
                    and call.func.value.id in self.exit_stacks
                    and len(call.args) == 1
                    and guard._is_self_attr(call.args[0], "_db_lock")
                ):
                    self.locked += (call.args[0].value.id,)

            def visit_Try(self, node: ast.Try) -> None:
                # An enter_context which raises cannot protect its error handler.
                saved = self.locked
                for statement in node.body:
                    self.visit(statement)
                for statement in node.orelse:
                    self.visit(statement)
                self.locked = saved
                for handler in node.handlers:
                    self.visit(handler)
                    self.locked = saved
                for statement in node.finalbody:
                    self.visit(statement)
                self.locked = saved

            def visit_If(self, node: ast.If) -> None:
                saved = self.locked
                self.visit(node.test)
                for statement in node.body:
                    self.visit(statement)
                self.locked = saved
                for statement in node.orelse:
                    self.visit(statement)
                self.locked = saved

            visit_While = visit_If

            def visit_For(self, node: ast.For) -> None:
                saved = self.locked
                self.generic_visit(node)
                self.locked = saved

            def visit_ClassDef(self, node) -> None:
                self.func_stack.append(node.name)
                self.generic_visit(node)
                self.func_stack.pop()

            def _visit_func(self, node) -> None:
                self.func_stack.append(node.name)
                saved, saved_stacks = self.locked, self.exit_stacks
                self.locked = ()  # function body runs at call time, not here
                self.exit_stacks = set()
                self.generic_visit(node)
                self.locked, self.exit_stacks = saved, saved_stacks
                self.func_stack.pop()

            visit_FunctionDef = _visit_func
            visit_AsyncFunctionDef = _visit_func

            def visit_Call(self, node: ast.Call) -> None:
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr in (
                    "execute",
                    "executemany",
                    "executescript",
                ):
                    where = ".".join(self.func_stack) or "<module>"
                    if (
                        guard._is_self_attr(func.value, "db")
                        and func.value.value.id not in self.locked
                    ):
                        violations.append(
                            f"line {node.lineno} ({where}): self.db.{func.attr}() outside "
                            "`with self._db_lock:` — use _fetch_all_locked/_fetch_one_locked "
                            "for reads or take the lock explicitly for writes"
                        )
                    elif guard._is_self_attr(func.value, "_db") and not (
                        self.func_stack and self.func_stack[-1] in guard._RAW_DB_ALLOWED
                    ):
                        violations.append(
                            f"line {node.lineno} ({where}): raw self._db.{func.attr}() outside "
                            f"{sorted(guard._RAW_DB_ALLOWED)} — go through self.db under _db_lock"
                        )
                self.generic_visit(node)

        Visitor().visit(tree)
        return violations

    @staticmethod
    def _runtime_sources() -> dict[str, str]:
        """Every ``vector_memory_runtime`` module's source, keyed by file name."""
        from kiro_crew import vector_memory

        root = Path(vector_memory.__file__).resolve().with_name("vector_memory_runtime")
        return {path.name: path.read_text(encoding="utf-8") for path in sorted(root.glob("*.py"))}

    def test_every_db_statement_is_lock_serialized(self) -> None:
        import ast
        import importlib
        import inspect

        from kiro_crew import vector_memory

        # The facade and every owner beside it (``from kiro_crew import vector_memory_<x>``);
        # the vector_memory_runtime modules have their own test below.
        owners = [
            importlib.import_module(f"kiro_crew.{alias.name}")
            for node in ast.parse(inspect.getsource(vector_memory)).body
            if isinstance(node, ast.ImportFrom) and node.module == "kiro_crew"
            for alias in node.names
            if alias.name.startswith("vector_memory_")
        ]
        assert owners, "the facade binds members from owners beside it"
        violations = []
        for module in (vector_memory, *owners):
            violations.extend(
                self._find_unserialized_statements(ast.parse(inspect.getsource(module)))
            )
        assert not violations, "unserialized sqlite statement(s):\n" + "\n".join(violations)

    def test_every_runtime_db_statement_is_lock_serialized(self) -> None:
        """The store's delegates run on ``store.db`` too, under the same rule. A
        raw ``store._db`` statement is refused (no runtime function is a lifecycle
        method), and neither handle may be bound to a local name that the guard
        would then not recognise as the store's connection."""
        import ast

        sources = self._runtime_sources()
        assert {"lessons.py", "embedding.py"} <= sources.keys()
        violations: list[str] = []
        for name, source in sources.items():
            tree = ast.parse(source)
            violations.extend(
                f"{name}: {item}" for item in self._find_unserialized_statements(tree)
            )
            for node in ast.walk(tree):
                if (
                    isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr))
                    and isinstance(node.value, ast.Attribute)
                    and (
                        self._is_self_attr(node.value, "db")
                        or self._is_self_attr(node.value, "_db")
                    )
                ):
                    violations.append(f"{name}:{node.lineno}: the connection bound to a name")
        assert not violations, "unserialized sqlite statement(s):\n" + "\n".join(violations)

    def test_guard_catches_a_seeded_runtime_violation(self) -> None:
        """Runtime-style code, with the store as a parameter, is checked too."""
        import ast

        seeded = ast.parse(
            "def bad(store):\n"
            "    return store.db.execute('SELECT 1').fetchone()\n"
            "def good(store):\n"
            "    with store._db_lock:\n"
            "        return store.db.execute('SELECT 1').fetchone()\n"
            "def committed(store, vec):\n"
            "    with store._vector_commit(vec):\n"
            "        store.db.execute('UPDATE x SET y = 1')\n"
            "def raw(store):\n"
            "    with store._db_lock:\n"
            "        return store._db.execute('SELECT 1')\n"
            "class VectorMemoryStore:\n"
            "    def merge_from(self, store):\n"
            "        with store._db_lock:\n"
            "            return self.db.execute('SELECT 1').fetchall()\n"
        )
        violations = self._find_unserialized_statements(seeded)
        assert len(violations) == 3
        assert "(bad)" in violations[0]
        assert "(raw)" in violations[1]
        # One store's lock does not serialize a statement on another store.
        assert "(VectorMemoryStore.merge_from)" in violations[2]

    def test_guard_catches_a_seeded_violation(self) -> None:
        """The guard itself must fail on an unlocked fetch — otherwise a refactor
        that breaks its With/function tracking would silently disarm it."""
        import ast

        seeded = ast.parse(
            "class S:\n"
            "    def bad(self):\n"
            "        return self.db.execute('SELECT 1').fetchone()\n"
            "    def good(self):\n"
            "        with self._db_lock:\n"
            "            return self.db.execute('SELECT 1').fetchone()\n"
            "    def sneaky(self):\n"
            "        with self._db_lock:\n"
            "            def inner():\n"
            "                return self.db.execute('SELECT 1').fetchone()\n"
            "            return inner\n"
            "    def raw(self):\n"
            "        return self._db.execute('SELECT 1').fetchone()\n"
        )
        violations = self._find_unserialized_statements(seeded)
        # `bad` is a plain unlocked fetch; `sneaky`'s inner function is defined
        # under the lock but runs at call time; `raw` bypasses the property
        # outside the allowed lifecycle methods. `good` must not be flagged.
        assert len(violations) == 3
        flagged = "\n".join(violations)
        assert "(S.bad)" in flagged
        assert "(S.sneaky.inner)" in flagged
        assert "(S.raw)" in flagged
        assert "S.good" not in flagged

    def test_guard_tracks_exit_stack_scope_and_acquisition(self) -> None:
        import ast

        tree = ast.parse(
            "class S:\n"
            "    def scopes(self, condition):\n"
            "        with ExitStack() as resources:\n"
            "            self.db.execute('before')\n"
            "            if condition:\n"
            "                resources.enter_context(self._db_lock)\n"
            "            self.db.execute('conditional')\n"
            "            try:\n"
            "                resources.enter_context(self._db_lock)\n"
            "                self.db.execute('guarded')\n"
            "            except OSError:\n"
            "                self.db.execute('failed admission')\n"
            "            resources.enter_context(self._db_lock)\n"
            "            self.db.execute('guarded again')\n"
            "            def later():\n"
            "                resources.enter_context(self._db_lock)\n"
            "                self.db.execute('captured closed stack')\n"
            "        self.db.execute('after')\n"
            "    def transaction(self):\n"
            "        with self._vector_commit([1.0]):\n"
            "            self.db.execute('guarded transaction')\n"
        )
        violations = self._find_unserialized_statements(tree)
        assert len(violations) == 5, violations
        assert sum("S.scopes.later" in item for item in violations) == 1
        assert not any("S.transaction" in item for item in violations)

    def test_guard_rejects_vector_commit_without_its_real_lock(self) -> None:
        import ast
        import inspect

        from kiro_crew import vector_memory

        source = inspect.getsource(vector_memory)
        acquisition = "resources.enter_context(self._db_lock)"
        assert source.count(acquisition) == 1
        violations = self._find_unserialized_statements(
            ast.parse(source.replace(acquisition, "pass"))
        )
        assert any("VectorMemoryStore._vector_commit" in item for item in violations), violations


@pytest.mark.xdist_group("vector_memory_concurrency")
class TestReaderConcurrency1947:
    """Stress the readers that take ``_db_lock`` on the shared connection
    (get_semantic, get_all_semantic, search_semantic, get_events,
    get_episodic_list, memory_stats, _get_episodic, get_rejection_stats)
    against concurrent writers.

    Same defect class: an unserialized statement racing a writer's
    implicit transaction corrupts the per-connection statement cache
    (sqlite3.InterfaceError "bad parameter or other API misuse") or silently
    corrupts row iteration. With every fetch routed through the locked helper,
    readers must raise NOTHING — dashboard/API callers surface any exception
    as a 500.
    """

    def test_previously_unlocked_readers_survive_concurrent_writes(
        self, tmp_path: Path, opened
    ) -> None:
        store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db"))
        store.init()
        # Seed both tables plus the event log so every reader has real rows.
        seeded_episodic_ids: list[str] = []
        for i in range(10):
            store.set_semantic(f"project.seed.k{i:02d}", f"alpha value {i}", 1.0, "tool")
            store.write_episodic(f"seeded episodic memory {i} about alpha beta topics")
        seeded_episodic_ids = [m["id"] for m in store.get_episodic_list(limit=10)]

        start_barrier = threading.Barrier(4)
        errors: list[BaseException] = []
        progress: dict[int, int] = {}

        def _writer(n: int, tag: str) -> None:
            tid = threading.get_ident()
            progress[tid] = 0
            # flake-ok: pre-existing barrier wait with no timeout, moved verbatim
            start_barrier.wait()
            try:
                for i in range(n):
                    store.set_semantic(f"project.stress.{tag}{i:03d}", f"gamma {i}", 1.0, "tool")
                    store.write_episodic(f"stress episodic {tag}{i:03d} gamma delta {i}")
                    progress[tid] += 1
            except BaseException as exc:  # noqa: BLE001 - capture any thread crash
                errors.append(exc)

        def _reader(n: int) -> None:
            tid = threading.get_ident()
            progress[tid] = 0
            # flake-ok: pre-existing barrier wait with no timeout, moved verbatim
            start_barrier.wait()
            try:
                for i in range(n):
                    store.get_semantic("project.seed.k00")
                    store.get_all_semantic(limit=20)
                    store.search_semantic("project.")
                    store.get_events(limit=20)
                    store.get_episodic_list(limit=20)
                    store.memory_stats()
                    store._get_episodic(seeded_episodic_ids[i % len(seeded_episodic_ids)])
                    store.get_rejection_stats()
                    progress[tid] += 1
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=_writer, args=(40, "a")),
            threading.Thread(target=_writer, args=(40, "b")),
            threading.Thread(target=_reader, args=(30,)),
            threading.Thread(target=_reader, args=(30,)),
        ]
        for t in threads:
            t.start()
        _join_stress_threads(threads, progress, "unlocked-reader/writer stress")
        assert not errors, f"concurrent reader/writer stress raised: {errors!r}"

        # Post-stress coherence: counts add up and a fresh read round-trips.
        stats = store.memory_stats()
        assert stats["semantic_active"] >= 90  # 10 seed + 2×40 stress writers
        assert store.get_semantic("project.stress.a000") is not None
