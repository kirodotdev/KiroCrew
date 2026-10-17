"""Tests for relevance-ranked lesson injection via ``get_lessons_context``."""

from __future__ import annotations

import struct
from collections.abc import Iterator
from pathlib import Path

import pytest

from kiro_crew import vector_memory
from kiro_crew.vector_memory import VectorMemoryStore
from kiro_crew.vector_memory_runtime.lessons import (
    LESSON_TRUNCATION_MARKER,
    truncate_explicit_lessons,
)

# Deliberately share no significant words, so write_lesson's topic-overlap
# dedup keeps all of them and each test controls the ordering it exercises.
TABS = "Prefer tabs over spaces in Makefiles"
MIGRATION = "Run the database migration before deploying"
FORCE_PUSH = "Never force push to a shared branch"
DIGEST = "Pin container images by digest rather than tag"
CERTIFICATE = "Rotate the signing certificate every ninety days"
ALL_RULES = (TABS, MIGRATION, FORCE_PUSH, DIGEST, CERTIFICATE)


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[VectorMemoryStore]:
    memory = VectorMemoryStore(db_path=tmp_path / "mem.db")
    memory.init()
    # Patch after schema setup; only lesson writes consume these distinct instants.
    ticks = iter(f"2026-01-01T00:00:00.{tick:06d}+00:00" for tick in range(1, 1000))
    monkeypatch.setattr(vector_memory, "_now_iso", lambda: next(ticks))
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

    @pytest.mark.parametrize("query", ["", "unrelated zebra xylophone"])
    def test_updating_an_older_lesson_moves_it_first(self, store, query):
        store.write_lesson(TABS)
        store.write_lesson(MIGRATION)
        assert shown(store.get_lessons_context(query_text=query)) == [MIGRATION, TABS]
        rows = store.get_lessons()
        original_keys = {row["key"] for row in rows}
        older = next(row for row in rows if TABS in row["value_json"])

        # The public editor path updates the same key, not a newly inserted lesson.
        assert store.set_semantic(older["key"], TABS, 1.0, "user_explicit") is None

        assert shown(store.get_lessons_context(query_text=query)) == [TABS, MIGRATION]
        assert {row["key"] for row in store.get_lessons()} == original_keys


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


def long_lesson(length: int) -> str:
    """A rule of *length* characters that ranks first for ``CERTIFICATE``.

    Its filler words share nothing with the other constants, so dedup keeps it
    beside them and lexical ranking puts it ahead of them for that query.
    """
    rule = CERTIFICATE + " " + " ".join(f"filler{index}" for index in range(length))
    return rule[:length]


# The share of ``VectorMemoryStore.recall``'s default cap that lessons get.
RECALL_CAP = 3000
LESSON_SHARE = RECALL_CAP // 3
SHORT_RULES = (TABS, MIGRATION, DIGEST)


class TestLongTopRankedLesson:
    """A long lesson ranked FIRST must not crowd out the shorter ones behind it."""

    @pytest.mark.parametrize("length", [900, 1200])
    def test_shorter_lessons_behind_it_still_render(
        self, store: VectorMemoryStore, length: int
    ) -> None:
        """900 fits the share alone but not with its frame; 1200 fits neither."""
        long = long_lesson(length)
        for rule in SHORT_RULES:
            store.write_lesson(rule)
        store.write_lesson(long)

        block = store.get_lessons_context(query_text=CERTIFICATE, cap=LESSON_SHARE)

        assert sorted(shown(block)) == sorted(SHORT_RULES)
        assert len(block) <= LESSON_SHARE

    def test_everything_that_fits_is_byte_identical_to_the_uncapped_render(
        self, store: VectorMemoryStore
    ) -> None:
        for rule in ALL_RULES:
            store.write_lesson(rule)

        capped = store.get_lessons_context(query_text=CERTIFICATE, cap=LESSON_SHARE)

        assert capped == store.get_lessons_context(query_text=CERTIFICATE)

    def test_two_lessons_at_an_exact_fit_cap_both_render(self, store: VectorMemoryStore) -> None:
        """A one-lesson render carries a counts line the full render does not.

        So at a cap equal to the full render, every partial render is over the
        cap and only the whole-block check keeps both lessons.
        """
        store.write_lesson(TABS)
        store.write_lesson(CERTIFICATE)
        uncapped = store.get_lessons_context(query_text=CERTIFICATE)

        capped = store.get_lessons_context(query_text=CERTIFICATE, cap=len(uncapped))

        assert capped == uncapped
        assert sorted(shown(capped)) == sorted((TABS, CERTIFICATE))


class TestRecallLessonShare:
    """``recall`` returns the best lessons that fit its share.

    A lone over-share lesson is cut behind a marker only while the share holds
    the block's frame, the marker and one character of text.
    """

    def test_short_lessons_are_returned_when_the_top_match_is_too_long(
        self, store: VectorMemoryStore
    ) -> None:
        for rule in SHORT_RULES:
            store.write_lesson(rule)
        store.write_lesson(long_lesson(900))

        result = store.recall(CERTIFICATE, cap=RECALL_CAP)

        assert sorted(shown(result["lessons_context"])) == sorted(SHORT_RULES)

    def test_a_lone_oversized_lesson_is_truncated_with_a_marker(
        self, store: VectorMemoryStore
    ) -> None:
        long = long_lesson(1200)
        store.write_lesson(long)

        result = store.recall(CERTIFICATE, cap=RECALL_CAP)

        lessons = result["lessons_context"]
        assert 0 < len(lessons) <= LESSON_SHARE
        (body,) = shown(lessons)
        assert body.endswith(LESSON_TRUNCATION_MARKER)
        assert long.startswith(body[: -len(LESSON_TRUNCATION_MARKER)])
        assert lessons.endswith("[End of learned corrections]\n")

    def test_lessons_that_fit_are_returned_whole(self, store: VectorMemoryStore) -> None:
        for rule in ALL_RULES:
            store.write_lesson(rule)

        result = store.recall(CERTIFICATE, cap=RECALL_CAP)

        assert sorted(shown(result["lessons_context"])) == sorted(ALL_RULES)
        assert "omitted" not in result["lessons_context"]
        assert LESSON_TRUNCATION_MARKER not in result["lessons_context"]

    @pytest.mark.parametrize(("cap", "kept"), [(483, True), (480, False)])
    def test_a_lone_lesson_is_kept_only_from_the_stated_cap(
        self, store: VectorMemoryStore, cap: int, kept: bool
    ) -> None:
        """The spec's 483 is the smallest cap whose share fits one in-scope lesson's frame."""
        store.write_lesson(long_lesson(1200))

        result = store.recall(CERTIFICATE, cap=cap)

        assert bool(result["lessons_context"]) is kept

    @pytest.mark.parametrize(
        ("query", "order", "cap", "kept"),
        [
            (CERTIFICATE, "most relevant", 651, True),
            (CERTIFICATE, "most relevant", 648, False),
            ("what is the status of", "most recent", 645, True),
            ("what is the status of", "most recent", 642, False),
        ],
    )
    def test_with_a_second_lesson_counted_it_is_kept_only_from_the_stated_cap(
        self, store: VectorMemoryStore, query: str, order: str, cap: int, kept: bool
    ) -> None:
        """The spec's 651, or 645 for a query with no recall terms, is the
        smallest cap whose share also fits the counts line."""
        store.write_lesson(long_lesson(1200))
        other = TABS + " " + " ".join(f"padding{index}" for index in range(1200))
        store.write_lesson(other[:1200])

        result = store.recall(query, cap=cap)

        lessons = result["lessons_context"]
        assert bool(lessons) is kept
        if kept:
            assert f"Showing 1 of 2 lessons, {order} first" in lessons
            assert len(lessons) <= cap // 3


class TestTruncateExplicitLessons:
    def test_a_block_within_the_cap_is_returned_unchanged(self) -> None:
        block = "[Learned corrections]\n- rule\n[End of learned corrections]\n"

        assert truncate_explicit_lessons(block, len(block)) == block

    def test_a_cap_too_small_for_the_frame_yields_nothing(self) -> None:
        block = "[Learned corrections]\n- " + "x" * 100 + "\n[End of learned corrections]\n"

        assert truncate_explicit_lessons(block, 30) == ""


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
    """Imported lessons are stored as a mapping, not a string."""

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
