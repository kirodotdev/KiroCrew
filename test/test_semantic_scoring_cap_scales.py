"""The semantic scoring cap scales with available memory.

A large store on a host with memory to spare must hold the resident scoring set
(and so keep the term index), instead of falling back to the per-call scan that
re-stems the whole population on every recall — the fallback that stalls turn
builds on large stores. A small host must keep the fixed 64 MiB floor and never
load a resident set it cannot hold. When available memory is unreadable the floor
is kept (fail safe).
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

from kiro_crew import subagent
from kiro_crew import vector_memory as vm
from kiro_crew.vector_memory import VectorMemoryStore

_DIM = 1024  # shipped embedding width; one blob is _DIM * 4 bytes


def _store(tmp_path: Path) -> VectorMemoryStore:
    store = VectorMemoryStore(db_path=tmp_path / "mem.db", embedding_dim=_DIM)
    store.init()
    return store


def _bulk_seed(store: VectorMemoryStore, n: int) -> None:
    """Insert *n* fact rows with real 1024-d embeddings straight through SQL.

    Direct insert (not ``set_semantic``) keeps a >64 MiB store fast enough for
    CI: the read path only needs valid rows in the columns the scoring scan
    selects (key, value_json, updated_at, embedding, source).
    """
    now = "2026-10-31T00:00:00+00:00"
    blob = struct.pack(f"<{_DIM}f", *([0.1] * _DIM))
    rows = [
        (
            f"project.item_{i}.fact",
            json.dumps(f"fact {i} tokyo deploy pipeline memory recall"),
            1.0,
            "user_explicit",
            now,
            now,
            0,
            blob,
        )
        for i in range(n)
    ]
    with store._db_lock:
        db = store.db
        db.execute("BEGIN")
        db.executemany(
            "INSERT OR REPLACE INTO semantic_memory "
            "(key, value_json, confidence, source, created_at, updated_at, is_deleted, embedding) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        db.execute("COMMIT")


def _scoring_bytes(store: VectorMemoryStore) -> int:
    """Lower bound on the resident set's accounted size, by the read path's own rule."""
    rows = store._fetch_all_locked(
        "SELECT key, value_json, updated_at, embedding, source FROM semantic_memory "
        "WHERE is_deleted = 0 AND key NOT LIKE 'lesson.%'"
    )
    total = 0
    for raw in rows:
        total += 256
        for value in dict(raw).values():
            if isinstance(value, str):
                total += len(value.encode("utf-8"))
            elif isinstance(value, bytes):
                total += len(value)
    return total


# ── the scaled cap itself ────────────────────────────────────────────────────


def test_cap_scales_with_available_memory_up_to_the_ceiling(monkeypatch) -> None:
    # A roomy host scales to a fraction of free memory, bounded by the ceiling.
    monkeypatch.setattr(subagent, "_available_memory_gb", lambda: 8.0)  # 8 GiB free
    expected = int(8 * 1024**3 * vm._SEMANTIC_SCORING_AVAIL_FRACTION)
    assert vm._semantic_scoring_max_bytes() == expected
    assert expected > vm._SEMANTIC_SCORING_MAX_BYTES  # above the floor

    monkeypatch.setattr(subagent, "_available_memory_gb", lambda: 256.0)  # huge host
    ceiling = vm._SEMANTIC_SCORING_CEILING_MULTIPLE * vm._SEMANTIC_SCORING_MAX_BYTES
    assert vm._semantic_scoring_max_bytes() == ceiling
    assert ceiling == 1024 * 1024 * 1024  # 1 GiB at the shipped 64 MiB floor


def test_cap_keeps_the_64_mib_floor_on_a_small_host(monkeypatch) -> None:
    # A host with little free memory never scales below the fixed floor.
    monkeypatch.setattr(subagent, "_available_memory_gb", lambda: 0.5)  # 512 MiB free
    assert vm._semantic_scoring_max_bytes() == vm._SEMANTIC_SCORING_MAX_BYTES


def test_cap_fails_safe_to_the_floor_when_memory_is_unreadable(monkeypatch) -> None:
    # Probe returns the -1.0 unreadable sentinel → keep the floor, do not scale.
    monkeypatch.setattr(subagent, "_available_memory_gb", lambda: -1.0)
    assert vm._semantic_scoring_max_bytes() == vm._SEMANTIC_SCORING_MAX_BYTES

    # Probe raises → same fail-safe.
    def _boom() -> float:
        raise RuntimeError("no probe")

    monkeypatch.setattr(subagent, "_available_memory_gb", _boom)
    assert vm._semantic_scoring_max_bytes() == vm._SEMANTIC_SCORING_MAX_BYTES


# ── the cap's effect on the resident scoring set ──────────────────────────────


def test_large_store_builds_resident_set_on_a_roomy_host(tmp_path: Path, monkeypatch) -> None:
    """RED ON MAIN: a store over 64 MiB builds the resident set once the cap scales.

    On origin/main the cap is the fixed 64 MiB, so a store this size is refused
    and ``_semantic_scoring`` stays ``None``. With the scaled cap and a roomy
    host it is materialized.
    """
    store = _store(tmp_path)
    _bulk_seed(store, 16000)  # ~70 MB of accounted scoring bytes
    assert (
        _scoring_bytes(store) > vm._SEMANTIC_SCORING_MAX_BYTES
    ), "store must exceed the 64 MiB floor"

    monkeypatch.setattr(subagent, "_available_memory_gb", lambda: 64.0)  # ample free memory
    store._semantic_scoring = None
    store._semantic_scoring_refused = None

    built = store._semantic_scoring_set()
    assert built is not None, "a store under the scaled cap must build the resident set"
    assert store._semantic_scoring is not None
    assert store._semantic_scoring_refused is None


def test_large_store_refused_on_a_small_host(tmp_path: Path, monkeypatch) -> None:
    """A low-memory host keeps the 64 MiB floor, so the same store is refused."""
    store = _store(tmp_path)
    _bulk_seed(store, 16000)
    assert _scoring_bytes(store) > vm._SEMANTIC_SCORING_MAX_BYTES

    monkeypatch.setattr(subagent, "_available_memory_gb", lambda: 0.5)  # 512 MiB free
    store._semantic_scoring = None
    store._semantic_scoring_refused = None

    built = store._semantic_scoring_set()
    assert built is not None, "the overflow path returns the rows transiently"
    # ...but it is NOT retained as resident, and the refused token is set.
    assert store._semantic_scoring is None
    assert store._semantic_scoring_refused is not None
