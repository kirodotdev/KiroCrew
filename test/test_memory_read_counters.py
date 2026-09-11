"""Read-volume counters on ``VectorMemoryStore`` — the read-volume observable.

The semantic-side searches re-read every row per call. The
stand-down on that issue found the defect real and HTTP-reachable but
UNASSERTABLE from outside the process: a SELECT moves neither ``data_version``
nor the WAL, the observability endpoint reported context sizes rather than rows
scanned, and wall-clock timing is not admissible evidence. These tests pin the
structural signal that closes that gap, so a live pod can assert the read
volume of a query path instead of inferring it from a stopwatch.

They do NOT assert the defect is fixed. ``test_a_second_identical_semantic_search_
scans_the_population_again`` deliberately pins today's re-read as VISIBLE; when
the fix lands, that test is the one that flips, which is the point of having it.

No embedder is wired here (``embed_fn`` stays None), so the semantic path scores
keyword-only and the episodic path takes its stdlib rung — both still perform the
population reads under test.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import kiro_crew.dashboard.handlers.memory as mem_mod
from kiro_crew.vector_memory import VectorMemoryStore

_COUNTER_KEYS = {
    "statements_executed",
    "rows_read",
    "semantic_rows_read",
    "semantic_full_scans",
    "episodic_rows_read",
    "episodic_full_scans",
}


def _store(tmp_path: Path, *, semantic: int = 0) -> VectorMemoryStore:
    store = VectorMemoryStore(db_path=tmp_path / "mem.db")
    store.init()
    for i in range(semantic):
        store.set_semantic(f"pref.item{i}", f"value {i} tea", 0.9, "user_explicit")
    return store


class TestCounterContract:
    def test_a_fresh_store_exposes_every_counter_at_zero(self, tmp_path: Path) -> None:
        """The counter-absent assertion: no ``read_counters`` at all fails here."""
        store = _store(tmp_path)
        assert store.read_counters() == dict.fromkeys(_COUNTER_KEYS, 0)

    def test_counters_are_monotonic_and_per_instance(self, tmp_path: Path) -> None:
        store = _store(tmp_path, semantic=2)
        store.get_semantic_context(query_text="tea")
        first = store.read_counters()
        assert first["semantic_full_scans"] == 1
        store.get_semantic_context(query_text="tea")
        assert store.read_counters()["semantic_full_scans"] == 2
        # A second store over the SAME file counts only its own reads, which is
        # what makes a two-process comparison meaningful rather than shared.
        other = VectorMemoryStore(db_path=tmp_path / "mem.db")
        other.init()
        assert other.read_counters()["semantic_full_scans"] == 0
        assert store.read_counters()["semantic_full_scans"] == 2

    def test_a_snapshot_does_not_alias_the_live_counters(self, tmp_path: Path) -> None:
        store = _store(tmp_path, semantic=1)
        snapshot = store.read_counters()
        store.get_semantic_context(query_text="tea")
        assert snapshot["semantic_full_scans"] == 0


class TestSemanticSurface:
    def test_the_full_read_branch_credits_every_row_it_materializes(self, tmp_path: Path) -> None:
        store = _store(tmp_path, semantic=4)
        before = store.read_counters()
        store.get_semantic_context(query_text="tea")
        after = store.read_counters()
        assert after["semantic_full_scans"] - before["semantic_full_scans"] == 1
        assert after["semantic_rows_read"] - before["semantic_rows_read"] == 4
        assert after["rows_read"] - before["rows_read"] >= 4

    def test_a_second_identical_semantic_search_scans_the_population_again(
        self, tmp_path: Path
    ) -> None:
        """The read-volume assertion, in the form a live pod can make.

        Two identical searches with no write in between read the population
        twice today. When the fix lands (resident scoring columns under a
        (generation, data_version) token), the second call
        stops scanning and these numbers stay flat — so this test is the ratchet
        that has to be updated by the fix, not silently satisfied by it.
        """
        store = _store(tmp_path, semantic=4)
        store.get_semantic_context(query_text="tea")
        one = store.read_counters()
        store.get_semantic_context(query_text="tea")
        two = store.read_counters()
        assert two["semantic_full_scans"] - one["semantic_full_scans"] == 1
        assert two["semantic_rows_read"] - one["semantic_rows_read"] == 4

    def test_the_recency_branch_is_not_counted_as_a_population_scan(self, tmp_path: Path) -> None:
        """No query means a bounded ``LIMIT`` read, which is a different shape."""
        store = _store(tmp_path, semantic=4)
        before = store.read_counters()
        store.get_semantic_context()
        after = store.read_counters()
        assert after["semantic_full_scans"] == before["semantic_full_scans"]
        assert after["semantic_rows_read"] == before["semantic_rows_read"]
        assert after["rows_read"] > before["rows_read"]

    def test_unbounded_get_lessons_is_a_population_scan_but_a_limited_one_is_not(
        self, tmp_path: Path
    ) -> None:
        """The other half: the ``_stored_similarity_scorer`` callers."""
        store = _store(tmp_path)
        store.write_lesson("always run the build before pushing")
        before = store.read_counters()
        store.get_lessons()
        unbounded = store.read_counters()
        assert unbounded["semantic_full_scans"] - before["semantic_full_scans"] == 1
        store.get_lessons(limit=1)
        limited = store.read_counters()
        assert limited["semantic_full_scans"] == unbounded["semantic_full_scans"]
        assert limited["rows_read"] > unbounded["rows_read"]


class TestEpisodicSurface:
    def test_an_episodic_search_credits_the_population_it_scans(self, tmp_path: Path) -> None:
        """Either episodic rung — resident build or per-call read — is credited.

        The resident set pays the population read once per invalidation
        and the per-call rung pays it once per search; both land on
        ``episodic_full_scans``, so the counter reads the same on a numpy install
        and a stock one, and the DIFFERENCE between them is exactly what the
        cross-call comparison shows.
        """
        store = _store(tmp_path)
        for i in range(3):
            store.write_episodic(f"a conversation fragment number {i} about tea")
        # Stored embeddings need an embedder; without one the rows carry no
        # vector, so drive the scan through a query embedding directly.
        before = store.read_counters()
        store._sqlite_vector_search([0.1, 0.2, 0.3], "tea", 5, None, False)
        after = store.read_counters()
        assert after["episodic_full_scans"] - before["episodic_full_scans"] == 1
        assert after["semantic_full_scans"] == before["semantic_full_scans"]


class TestAllTablesTotals:
    def test_a_keyed_read_counts_a_statement_without_any_population_scan(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path, semantic=1)
        before = store.read_counters()
        store.get_semantic("pref.item0")
        after = store.read_counters()
        assert after["statements_executed"] > before["statements_executed"]
        assert after["semantic_full_scans"] == before["semantic_full_scans"]
        assert after["episodic_full_scans"] == before["episodic_full_scans"]

    def test_a_miss_counts_the_statement_but_no_rows(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        before = store.read_counters()
        assert store.get_semantic("pref.absent") is None
        after = store.read_counters()
        assert after["statements_executed"] - before["statements_executed"] == 1
        assert after["rows_read"] == before["rows_read"]


def _request(store: VectorMemoryStore, *, query: dict[str, str]) -> Any:
    """A faked ``web.Request`` wired to a REAL store.

    The rest of the memory handler suite mocks the store; here the store must be
    real, because the assertion under test is that the counters a live gateway
    would report reach the response body.
    """
    mem = MagicMock()
    mem.vector_store = store
    state = MagicMock()
    state.context_builder = MagicMock(memory=mem)
    state._slots = {}
    state._restricted_keys = set()
    req = MagicMock()
    req.app = {"state": state}
    req.method = "GET"
    req.query = query
    req.match_info = {}
    req.headers = {}
    req.can_read_body = False
    req.json = AsyncMock(return_value=None)
    return req


class TestObservabilityEndpoint:
    @pytest.mark.asyncio
    async def test_the_response_carries_a_reads_object(self, tmp_path: Path) -> None:
        store = _store(tmp_path, semantic=4)
        body = json.loads(
            (await mem_mod.api_memory_observability(_request(store, query={"q": "tea"}))).text or ""
        )
        assert set(body["reads"]) == _COUNTER_KEYS

    @pytest.mark.asyncio
    async def test_a_queried_request_reports_its_own_semantic_scan(self, tmp_path: Path) -> None:
        """The counters are read LAST, so the request's own scan is included.

        That ordering is what makes the endpoint usable as the read-volume probe: an
        agent calls it twice with the same ``q`` and compares the two objects.
        """
        store = _store(tmp_path, semantic=4)
        first = json.loads(
            (await mem_mod.api_memory_observability(_request(store, query={"q": "tea"}))).text or ""
        )["reads"]
        second = json.loads(
            (await mem_mod.api_memory_observability(_request(store, query={"q": "tea"}))).text or ""
        )["reads"]
        assert first["semantic_full_scans"] >= 1
        assert first["semantic_rows_read"] >= 4
        # Identical second request, and the endpoint shows it paid for the
        # population again — the observation this endpoint makes possible.
        assert second["semantic_full_scans"] > first["semantic_full_scans"]
        assert second["semantic_rows_read"] - first["semantic_rows_read"] >= 4
