"""Tests for relevance-ranked lesson injection via ``get_lessons_context``."""

from __future__ import annotations

import struct
from collections.abc import Iterator
from pathlib import Path

import pytest

from kiro_crew import vector_memory
from kiro_crew.vector_memory import VectorMemoryStore

# Deliberately share no significant words, so write_lesson's topic-overlap
# dedup keeps all of them and each test controls the ordering it exercises.
TABS = "Prefer tabs over spaces in Makefiles"
MIGRATION = "Run the database migration before deploying"
FORCE_PUSH = "Never force push to a shared branch"
DIGEST = "Pin container images by digest rather than tag"
CERTIFICATE = "Rotate the signing certificate every ninety days"
ALL_RULES = (TABS, MIGRATION, FORCE_PUSH, DIGEST, CERTIFICATE)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[VectorMemoryStore]:
    memory = VectorMemoryStore(db_path=tmp_path / "mem.db")
    memory.init()
    yield memory
    memory.close()


def shown(block: str) -> list[str]:
    """The lesson bodies rendered in *block*, in order."""
    return [line[2:] for line in block.splitlines() if line.startswith("- ")]


class TestRelevanceOrdering:
    def test_relevant_lesson_outranks_newer_ones(self, store: VectorMemoryStore) -> None:
        """Keyword overlap wins over write order."""
        for rule in (MIGRATION, FORCE_PUSH, TABS):
            store.write_lesson(rule)

        block = store.get_lessons_context(query_text="how do I run a database migration")

        assert shown(block)[0] == MIGRATION

    def test_order_is_labelled_when_lessons_are_omitted(self, store: VectorMemoryStore) -> None:
        """The scope line names the ordering so a truncated block is unambiguous."""
        for rule in ALL_RULES:
            store.write_lesson(rule)

        ranked = store.get_lessons_context(query_text=MIGRATION, cap=250)
        recent = store.get_lessons_context(cap=250)

        assert "most relevant first" in ranked
        assert "most recent first" in recent

    def test_unmatched_query_falls_back_to_recency(self, store: VectorMemoryStore) -> None:
        """A query matching nothing leaves the newest-first order intact."""
        store.write_lesson(TABS)
        store.write_lesson(MIGRATION)

        block = store.get_lessons_context(query_text="unrelated zebra xylophone")

        assert shown(block) == [MIGRATION, TABS]

    def test_no_query_keeps_recency_order(self, store: VectorMemoryStore) -> None:
        store.write_lesson(TABS)
        store.write_lesson(MIGRATION)

        block = store.get_lessons_context()

        assert shown(block) == [MIGRATION, TABS]
        assert "most recent first" not in block  # nothing omitted, so no scope line


class TestCharacterBudget:
    def test_unbounded_cap_shows_every_lesson(self, store: VectorMemoryStore) -> None:
        for rule in ALL_RULES:
            store.write_lesson(rule)

        block = store.get_lessons_context()

        assert len(shown(block)) == len(ALL_RULES)
        assert "omitted" not in block

    def test_cap_is_respected_and_omissions_reported(self, store: VectorMemoryStore) -> None:
        for rule in ALL_RULES:
            store.write_lesson(rule)

        block = store.get_lessons_context(cap=250)

        assert len(block) <= 250
        assert 0 < len(shown(block)) < len(ALL_RULES)
        assert f"of {len(ALL_RULES)} lessons" in block
        assert "omitted." in block

    def test_one_lesson_survives_an_unmeetable_cap(self, store: VectorMemoryStore) -> None:
        """A cap smaller than any single lesson still yields the top-ranked one."""
        store.write_lesson(TABS)
        store.write_lesson(MIGRATION)

        block = store.get_lessons_context(query_text=MIGRATION, cap=1)

        assert shown(block) == [MIGRATION]

    def test_a_lesson_too_long_to_fit_does_not_discard_shorter_ones(
        self, store: VectorMemoryStore
    ) -> None:
        """An oversized lesson is skipped, not treated as the end of the budget.

        Stopping at the first lesson that does not fit would throw away every
        shorter lesson ranked behind it, wasting the remaining budget. The
        oversized rule is written second of three so it lands in the MIDDLE of
        the recency order, which is the only arrangement where skipping and
        stopping differ.
        """
        oversized = "Avoid " + "verbosity " * 60
        store.write_lesson(MIGRATION)  # ranked last
        store.write_lesson(oversized)  # ranked middle, cannot fit
        store.write_lesson(TABS)  # ranked first, fits

        block = store.get_lessons_context(cap=500)

        assert shown(block) == [TABS, MIGRATION]
        assert oversized not in shown(block)

    def test_empty_store_yields_nothing(self, store: VectorMemoryStore) -> None:
        assert store.get_lessons_context(query_text="anything", cap=1000) == ""


class TestVectorScoring:
    def test_query_is_embedded_once_and_row_vectors_reused(self, store: VectorMemoryStore) -> None:
        """Ranking reads stored embeddings instead of re-embedding each lesson."""
        vectors = {MIGRATION: [1.0, 0.0, 0.0], TABS: [0.0, 1.0, 0.0]}
        embedded: list[str] = []

        def embed(text: str) -> list[float]:
            embedded.append(text)
            return vectors.get(text, [0.0, 0.0, 1.0])

        store.embed_fn = embed
        store.write_lesson(TABS)
        store.write_lesson(MIGRATION)
        embedded.clear()

        block = store.get_lessons_context(query_text=MIGRATION)

        assert embedded == [MIGRATION]
        assert shown(block)[0] == MIGRATION

    def test_missing_row_embedding_degrades_to_keywords(self, store: VectorMemoryStore) -> None:
        """A lesson stored without a vector is still ranked on keyword overlap."""
        store.write_lesson(MIGRATION)
        store.write_lesson(TABS)
        store.embed_fn = lambda text: [1.0, 0.0, 0.0]

        block = store.get_lessons_context(query_text="database migration deploying")

        assert shown(block)[0] == MIGRATION


class TestStoredVectorComparability:
    """The per-query scorer must not let a row win on shape or magnitude."""

    def test_mismatched_dimension_vector_cannot_win_by_truncation(
        self, store: VectorMemoryStore
    ) -> None:
        """A vector of another dimensionality is incomparable, not a match.

        Rows written under a previous embedding model keep that model's
        dimensionality. Comparing one against a query from the current model used
        to score only the leading elements while each norm still used its own
        full vector, so the shorter row could reach a perfect 1.0 and outrank the
        row that genuinely matches.

        The blob is rewritten directly because that is the only way the mismatch
        arises: ``write_lesson`` embeds every row with whatever ``embed_fn`` is
        bound at the time, and handing it a short vector up front instead trips
        the dedup comparison — which truncates the same way — so the row is
        treated as a duplicate and never stored at all.
        """
        probe = "unrelated probe phrasing"
        vectors = {
            TABS: [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0],
            MIGRATION: [1.0, 1.0, 1.0, 1.0, 1.0, 0.0, 0.0, 0.0],
            probe: [1.0, 1.0, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0],
        }
        store.embed_fn = lambda text: vectors.get(text, [0.0] * 8)
        store.write_lesson(TABS)
        store.write_lesson(MIGRATION)
        # Leave TABS in a 4-dimensional space. Truncated against the query's
        # first four elements it scores 1.0; compared honestly it is unrelated.
        stale = struct.pack("4f", 1.0, 1.0, 1.0, 1.0)
        key = next(row["key"] for row in store.get_lessons() if TABS in row["value_json"])
        store.db.execute(
            "UPDATE semantic_memory SET embedding = ? WHERE key = ?", (stale, key)
        )
        store.db.commit()

        block = store.get_lessons_context(query_text=probe)

        assert shown(block) == [MIGRATION, TABS], (
            "a row embedded in another dimensionality was scored as a match"
        )

    def test_row_vector_magnitude_does_not_decide_ranking(
        self, store: VectorMemoryStore
    ) -> None:
        """Stored vectors are un-normalized, so both norms must be divided out.

        A plain inner product would rank the long, badly-aligned vector above the
        short, perfectly-aligned one.
        """
        probe = "unrelated probe phrasing"
        vectors = {
            MIGRATION: [1.0, 0.0, 0.0],  # unit length, points at the query
            TABS: [3.0, 4.0, 0.0],  # length 5, only 0.6 cosine
            probe: [1.0, 0.0, 0.0],
        }
        store.embed_fn = lambda text: vectors.get(text, [0.0, 0.0, 1.0])
        store.write_lesson(MIGRATION)
        store.write_lesson(TABS)

        block = store.get_lessons_context(query_text=probe)

        assert shown(block)[0] == MIGRATION, "a longer vector outranked a better-aligned one"

    def test_ranking_is_identical_without_numpy(
        self, store: VectorMemoryStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """numpy is an optional dependency, so the stdlib path must agree with it."""
        probe = "unrelated probe phrasing"
        vectors = {
            TABS: [0.9, 0.1, 0.2],
            MIGRATION: [0.2, 0.9, 0.1],
            FORCE_PUSH: [0.4, 0.4, 0.5],
            DIGEST: [0.1, 0.2, 0.9],
            probe: [0.7, 0.5, 0.3],
        }
        store.embed_fn = lambda text: vectors.get(text, [0.0, 0.0, 0.0])
        for rule in (TABS, MIGRATION, FORCE_PUSH, DIGEST):
            store.write_lesson(rule)

        with_numpy = shown(store.get_lessons_context(query_text=probe))
        monkeypatch.setattr(vector_memory, "_HAS_NUMPY", False)
        without_numpy = shown(store.get_lessons_context(query_text=probe))

        assert without_numpy == with_numpy
        assert len(with_numpy) == 4


class TestImportedLessonShapes:
    """Imported lessons are stored as a mapping, not a string (see #2656)."""

    def test_mapping_lesson_renders_as_its_rule_and_ranks(
        self, store: VectorMemoryStore
    ) -> None:
        store.write_lesson(TABS)
        store.set_semantic(
            "lesson.imported01",
            {"rule": MIGRATION, "category": "knowledge", "negative": None},
            1.0,
            "user_explicit",
        )

        block = store.get_lessons_context(query_text="database migration deploying")

        assert shown(block)[0] == MIGRATION
        assert "'rule'" not in block

    def test_unrenderable_lesson_is_excluded_from_the_counts(
        self, store: VectorMemoryStore
    ) -> None:
        """A shape with no rule is skipped, so it cannot be reported as omitted."""
        store.write_lesson(TABS)
        store.set_semantic("lesson.imported02", {"category": "knowledge"}, 1.0, "user_explicit")

        block = store.get_lessons_context()

        assert shown(block) == [TABS]
        assert "omitted" not in block
