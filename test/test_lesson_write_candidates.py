"""A lesson write compares against candidate rows, not every stored lesson.

A full scan loads all active lessons and runs the containment, keyword-overlap and cosine
checks on each (cost linear in the lesson count). ``lesson_candidates`` nominates the rows
that could trip a check and ``write_lesson`` runs the same checks on them.

These tests pin: the write reads a handful of rows from a 5,000-lesson store; its verdicts
equal a full scan's over a mixed store; the index follows deletes, re-submits and another
connection's writes; no numpy falls back to the scan; and a write embeds no stored lesson.
"""

from __future__ import annotations

import json
import random
import shutil
import struct
from pathlib import Path

import pytest

from kiro_crew import vector_memory as vm
from kiro_crew.vector_memory import LessonWriteOutcome, VectorMemoryStore
from kiro_crew.vector_memory_runtime import lesson_candidates

_DIM = 32
_STAMP = "2026-01-01T00:00:00+00:00"


def _open(path: Path) -> VectorMemoryStore:
    store = VectorMemoryStore(db_path=path, embedding_dim=_DIM)
    store.init()
    return store


def _insert(
    store: VectorMemoryStore,
    rows: list[tuple[str, str, list[float] | None]],
    *,
    automated: frozenset[int] = frozenset(),
) -> None:
    """Store lesson rows directly: ``(rule, scope or '', vector)``, oldest first.

    A row whose index is in *automated* is stored as a 0.9-confidence consolidation lesson.
    """
    with store._db_lock:
        for i, (rule, scope, vector) in enumerate(rows):
            value: dict[str, object] = {"rule": rule, "category": "knowledge", "negative": None}
            if scope:
                value["repo_scope"] = scope
            blob = struct.pack(f"{_DIM}f", *vector) if vector else None
            stamp = f"2026-01-01T{i // 3600 % 24:02d}:{i // 60 % 60:02d}:{i % 60:02d}+00:00"
            source, confidence = (
                ("consolidation", 0.9) if i in automated else ("user_explicit", 1.0)
            )
            store.db.execute(
                "INSERT INTO semantic_memory (key, value_json, confidence, source, created_at, "
                "updated_at, embedding) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (f"lesson.{i:032x}", json.dumps(value), confidence, source, stamp, stamp, blob),
            )
        store.db.commit()
    store._invalidate_semantic_scoring()


def _vector(rnd: random.Random) -> list[float]:
    return [rnd.gauss(0, 1) for _ in range(_DIM)]


def _rules(rnd: random.Random, count: int, words: int) -> list[str]:
    vocab = [f"term{i:04d}x" for i in range(2000)]
    return [" ".join(rnd.sample(vocab, words)) for _ in range(count)]


def test_a_write_reads_a_few_rows_not_all_of_a_5000_lesson_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rnd = random.Random(1)
    store = _open(tmp_path / "m.db")
    try:
        rules = _rules(rnd, 5000, 9)
        _insert(store, [(rule, "", _vector(rnd)) for rule in rules])
        # The first write scans; the second builds the index (the one full read).
        for warm in ("scan first", "build the index second"):
            assert store.write_lesson(warm, rule_emb=_vector(rnd), rule_emb_resolved=True)

        def _no_scan(*_args: object, **_kwargs: object) -> list[dict]:
            raise AssertionError("a lesson write must not call get_lessons()")

        monkeypatch.setattr(store, "get_lessons", _no_scan)
        before = store.read_counters()
        result = store.write_lesson(
            "a brand new rule about nothing stored", rule_emb=_vector(rnd), rule_emb_resolved=True
        )
        after = store.read_counters()

        assert result.outcome is LessonWriteOutcome.INSERTED
        read = {k: after[k] - before[k] for k in after}
        # The write's own key lookup plus its nominated neighbours: tens, not 5,000.
        assert max(read.values()) < 200, read
        assert store.count_lessons() == 5003
    finally:
        store.close()


@pytest.mark.parametrize("source", ["user_explicit", "consolidation"])
def test_verdicts_equal_a_full_scan_over_a_mixed_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    """The same writes against copies of one store give the same verdict and survivors."""
    rnd = random.Random(11)
    words = [f"w{i:02d}x" for i in range(60)]
    word_vec = {w: _vector(rnd) for w in words}

    def _text_and_vector(shorter: bool = False) -> tuple[str, list[float]]:
        picked = rnd.sample(words, rnd.randint(2, 4) if shorter else rnd.randint(3, 9))
        vec = [sum(word_vec[w][d] for w in picked) for d in range(_DIM)]
        return " ".join(picked), vec

    seed_rows: list[tuple[str, str, list[float] | None]] = []
    for i in range(400):
        text, vec = _text_and_vector()
        seed_rows.append((text, "org/repo" if i % 5 == 0 else "", None if i % 11 == 0 else vec))
    base = tmp_path / "base.db"
    seeded = _open(base)
    _insert(seeded, seed_rows)
    seeded.close()
    probes = [_text_and_vector(shorter=i % 3 == 0) for i in range(80)]
    # Probes that copy, contain and re-submit stored rules.
    probes += [(seed_rows[i][0], word_vec[words[0]]) for i in (3, 40, 77)]
    probes += [(seed_rows[i][0] + " extra", word_vec[words[1]]) for i in (5, 6, 120)]
    # Words nothing stored shares, a vector a hair from a stored row's: only cosine finds it.
    for n, i in enumerate((1, 2, 7, 13, 21, 30)):
        stored = seed_rows[i][2]
        assert stored is not None
        probes.append((f"qzx{n}a qzx{n}b qzx{n}c", [x + rnd.gauss(0, 0.05) for x in stored]))

    def _run(path: Path, use_index: bool) -> tuple[list[tuple], list[str]]:
        with monkeypatch.context() as patched:
            if not use_index:
                patched.setattr(lesson_candidates, "candidate_lesson_rows", lambda *a, **k: None)
            store = _open(path)
            try:
                out = []
                for i, (text, vec) in enumerate(probes):
                    scope = "org/repo" if i % 4 == 0 else None
                    result = store.write_lesson(
                        text,
                        source=source,
                        repo_scope=scope,
                        rule_emb=vec,
                        rule_emb_resolved=True,
                    )
                    out.append((result.outcome, result.reason, result.superseded, result.winner))
                keys = sorted((e["key"], e["value_json"]) for e in store.get_lessons())
                return out, [f"{k} {v}" for k, v in keys]
            finally:
                store.close()

    indexed_path, scan_path = tmp_path / "indexed.db", tmp_path / "scan.db"
    shutil.copy(base, indexed_path)
    shutil.copy(base, scan_path)
    indexed = _run(indexed_path, use_index=True)
    scanned = _run(scan_path, use_index=False)

    assert indexed[0] == scanned[0]
    assert indexed[1] == scanned[1]
    outcomes = {row[0] for row in scanned[0]}
    assert LessonWriteOutcome.INSERTED in outcomes
    assert any(row[3] or row[2] for row in scanned[0]), "the probes must trigger some dedup"


def test_the_index_follows_a_delete_and_a_resubmit(tmp_path: Path) -> None:
    rnd = random.Random(2)
    store = _open(tmp_path / "m.db")
    try:
        _insert(store, [(rule, "", _vector(rnd)) for rule in _rules(rnd, 50, 6)])
        vec = _vector(rnd)
        rule = "Quote every shell argument you interpolate"
        assert store.write_lesson(rule, rule_emb=vec, rule_emb_resolved=True).outcome is (
            LessonWriteOutcome.INSERTED
        )
        again = store.write_lesson(rule, rule_emb=vec, rule_emb_resolved=True)
        assert again.outcome is LessonWriteOutcome.UNCHANGED
        assert store.delete_lesson(rule, exact=True)
        back = store.write_lesson(rule, rule_emb=vec, rule_emb_resolved=True)
        assert back.outcome is LessonWriteOutcome.INSERTED
        # A covered write is declined from the index too.
        covered = store.write_lesson(
            "shell argument", rule_emb=_vector(rnd), rule_emb_resolved=True
        )
        assert covered.outcome is LessonWriteOutcome.DEDUPED
        assert covered.reason == "substring_covered"
    finally:
        store.close()


def test_the_index_sees_another_connections_write(tmp_path: Path) -> None:
    rnd = random.Random(3)
    path = tmp_path / "m.db"
    first = _open(path)
    second = _open(path)
    try:
        _insert(first, [(rule, "", _vector(rnd)) for rule in _rules(rnd, 30, 6)])
        for warm in ("scan first", "build the index second"):
            assert first.write_lesson(warm, rule_emb=_vector(rnd), rule_emb_resolved=True)
        vec = _vector(rnd)
        assert second.write_lesson(
            "Never push to the protected branch", rule_emb=vec, rule_emb_resolved=True
        )
        result = first.write_lesson(
            "Never push to the protected branch", rule_emb=vec, rule_emb_resolved=True
        )
        assert result.outcome is LessonWriteOutcome.UNCHANGED
    finally:
        first.close()
        second.close()


def test_without_numpy_the_write_scans_every_lesson(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rnd = random.Random(4)
    store = _open(tmp_path / "m.db")
    try:
        _insert(store, [("Quote every shell argument", "", _vector(rnd))])
        monkeypatch.setattr(vm, "_HAS_NUMPY", False)
        result = store.write_lesson("quote every shell argument you interpolate")
        assert result.outcome is LessonWriteOutcome.INSERTED
        assert result.superseded == ("Quote every shell argument",)
    finally:
        store.close()


def test_a_write_embeds_no_stored_lesson(tmp_path: Path) -> None:
    rnd = random.Random(5)
    store = _open(tmp_path / "m.db")
    try:
        _insert(store, [(rule, "", None) for rule in _rules(rnd, 20, 6)])
        calls: list[str] = []

        def _embed(text: str) -> list[float]:
            calls.append(text)
            return _vector(rnd)

        store.embed_fn = _embed
        assert store.write_lesson("a rule about something else entirely")
        assert calls == ["a rule about something else entirely"]
        embedded = [e for e in store.get_lessons() if e["embedding"]]
        assert [json.loads(e["value_json"])["rule"] for e in embedded] == [calls[0]]
    finally:
        store.close()


def test_an_index_failure_costs_the_scan_not_the_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rnd = random.Random(6)
    store = _open(tmp_path / "m.db")
    try:
        _insert(store, [("Quote every shell argument", "", _vector(rnd))])
        for warm in ("scan first", "build the index second"):
            assert store.write_lesson(warm)

        def _boom(*_args: object, **_kwargs: object) -> list[dict]:
            raise RuntimeError("index unavailable")

        monkeypatch.setattr(lesson_candidates, "_fresh_rows", _boom)
        result = store.write_lesson("quote every shell argument you interpolate")
        assert result.outcome is LessonWriteOutcome.INSERTED
        assert result.superseded == ("Quote every shell argument",)
    finally:
        store.close()


def test_a_row_written_while_the_index_builds_is_not_missed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write landing between the build's read and its install is patched in."""
    rnd = random.Random(7)
    store = _open(tmp_path / "m.db")
    try:
        _insert(store, [(rule, "", _vector(rnd)) for rule in _rules(rnd, 10, 6)])
        assert store.write_lesson("the first write scans")
        real_build = lesson_candidates._build

        def _build_then_write(target: VectorMemoryStore, version: int):
            built = real_build(target, version)
            # An odd key, so only the text (not the write's own key) can find the row.
            target.set_semantic(
                "lesson.written_during_build",
                {"rule": "written during the build", "category": "knowledge", "negative": None},
                1.0,
                "user_explicit",
            )
            return built

        monkeypatch.setattr(lesson_candidates, "_build", _build_then_write)
        result = store.write_lesson("WRITTEN during the BUILD")
        assert result.outcome is LessonWriteOutcome.UNCHANGED
        assert store.count_lessons() == 12
    finally:
        store.close()


def test_a_write_inside_the_db_lock_does_not_wait_for_a_build(tmp_path: Path) -> None:
    """A build in flight takes ``_db_lock`` last, so a holder of it must scan instead of waiting."""
    rnd = random.Random(8)
    store = _open(tmp_path / "m.db")
    try:
        _insert(store, [("Quote every shell argument", "", _vector(rnd))])
        assert store.write_lesson("scan first")
        with lesson_candidates._BUILD_LOCK, store._db_lock:
            result = store.write_lesson("quote every shell argument you interpolate")
        assert result.outcome is LessonWriteOutcome.INSERTED
        assert result.superseded == ("Quote every shell argument",)
    finally:
        store.close()


def test_a_published_vector_reaches_the_index_after_another_write_reconciled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second write lands between a row's commit and its vector, then the vector lands."""
    rnd = random.Random(9)
    store = _open(tmp_path / "m.db")
    try:
        _insert(store, [(rule, "", _vector(rnd)) for rule in _rules(rnd, 20, 6)])
        for warm in ("scan first", "build the index second"):
            assert store.write_lesson(warm, rule_emb=_vector(rnd), rule_emb_resolved=True)
        real_commit = store._vector_commit
        interleaved: list[bool] = []

        def _commit_after_a_second_write(vector, **kwargs):
            if not interleaved:
                interleaved.append(True)
                other = store.write_lesson(
                    "a second writer lands here", rule_emb=_vector(rnd), rule_emb_resolved=True
                )
                assert other.outcome is LessonWriteOutcome.INSERTED
            return real_commit(vector, **kwargs)

        monkeypatch.setattr(store, "_vector_commit", _commit_after_a_second_write)
        vec = _vector(rnd)
        assert store.write_lesson("alpha beta gamma", rule_emb=vec, rule_emb_resolved=True)
        assert interleaved

        near = [x + rnd.gauss(0, 0.05) for x in vec]
        result = store.write_lesson(
            "wholly different words here",
            source="consolidation",
            rule_emb=near,
            rule_emb_resolved=True,
        )
        assert result.outcome is LessonWriteOutcome.DEDUPED
        assert result.reason == "semantic_similarity"
    finally:
        store.close()


def test_a_vector_the_backfill_sweep_writes_reaches_the_index(tmp_path: Path) -> None:
    rnd = random.Random(10)
    vec = _vector(rnd)
    store = _open(tmp_path / "m.db")
    try:
        _insert(store, [(rule, "", _vector(rnd)) for rule in _rules(rnd, 20, 6)])
        assert store.write_lesson("alpha beta gamma", rule_emb=None, rule_emb_resolved=True)
        for warm in ("scan first", "build the index second"):
            assert store.write_lesson(warm, rule_emb=_vector(rnd), rule_emb_resolved=True)

        store.embed_fn = lambda text: list(vec)
        store.backfill_missing_embeddings(pace=False)
        near = [x + rnd.gauss(0, 0.05) for x in vec]
        result = store.write_lesson(
            "wholly different words here",
            source="consolidation",
            rule_emb=near,
            rule_emb_resolved=True,
        )
        assert result.outcome is LessonWriteOutcome.DEDUPED
        assert result.reason == "semantic_similarity"
    finally:
        store.close()


def test_a_row_that_outranks_the_write_is_found_beyond_the_neighbour_cap(tmp_path: Path) -> None:
    """64 closer automated rows must not hide the owner's lesson from the authority veto."""
    rnd = random.Random(11)
    query = _vector(rnd)
    size = sum(x * x for x in query) ** 0.5 / len(query) ** 0.5
    rows: list[tuple[str, str, list[float] | None]] = [
        (f"automated lesson number {i} qq{i}", "", [x + rnd.gauss(0, 0.02 * size) for x in query])
        for i in range(70)
    ]
    # Cosine about 0.9 with the query: farther in sketch distance than every automated row.
    rows.append(("the owner's own lesson zz", "", [x + rnd.gauss(0, 0.45 * size) for x in query]))
    store = _open(tmp_path / "m.db")
    try:
        _insert(store, rows, automated=frozenset(range(70)))
        for warm in ("scan first", "build the index second"):
            assert store.write_lesson(warm, rule_emb=_vector(rnd), rule_emb_resolved=True)
        before = store.count_lessons()
        result = store.write_lesson(
            "words nothing stored shares",
            source="consolidation",
            rule_emb=query,
            rule_emb_resolved=True,
        )
        assert result.outcome is LessonWriteOutcome.DEDUPED
        assert result.reason == "semantic_similarity"
        assert result.superseded == ()
        assert store.count_lessons() == before
    finally:
        store.close()


def test_a_bulk_change_during_a_build_keeps_the_stale_index_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rnd = random.Random(12)
    store = _open(tmp_path / "m.db")
    try:
        _insert(store, [(rule, "", _vector(rnd)) for rule in _rules(rnd, 10, 6)])
        assert store.write_lesson("the first write scans")
        real_build = lesson_candidates._build

        def _build_then_change(target: VectorMemoryStore, version: int):
            built = real_build(target, version)
            target._invalidate_semantic_scoring()
            return built

        monkeypatch.setattr(lesson_candidates, "_build", _build_then_change)
        assert store.write_lesson("the second write builds").outcome is LessonWriteOutcome.INSERTED
        assert lesson_candidates._state(store).index is None
        # The next write builds a fresh index and still dedups.
        monkeypatch.setattr(lesson_candidates, "_build", real_build)
        again = store.write_lesson("the second write builds")
        assert again.outcome is LessonWriteOutcome.UNCHANGED
        assert lesson_candidates._state(store).index is not None
    finally:
        store.close()
