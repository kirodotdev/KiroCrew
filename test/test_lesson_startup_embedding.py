"""Startup lessons rank with the vector the activity block already computed.

On a V1 session whose activity block embeds the first message, the prompt
builder reuses that vector (``startup_lesson_query``, served from the shared
embed cache) for every render of the lessons block. These cases pin that the
vector reaches the ranking, that a render repeated to fit the protected ceiling
does not embed again, that every row is scored on one weighted scale once there
is a vector, that every row takes the same keyword half (the capped overlap
count) with its cosine clamped at 0 as the vector term, so a row the vector
says nothing about (no comparable stored vector, or a cosine at or below 0)
is not measured against the best row in the set, gaining a vector never lowers
a row, rows tied on the hybrid score are ordered by word rarity rather than
recency, and a store in which no row has a positive cosine ranks exactly as
no vector does, and that every way the vector can be missing or stale
ranks lexically instead of failing the build, and a missing embedder is never
loaded from its factory. ``test_memory_v1_golden`` pins the inference count and
that ``inject_activity: false`` still embeds nothing.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kiro_crew._sqlite_compat import sqlite3
from kiro_crew.context import ContextBuilder
from kiro_crew.context_assembly import budget as context_budget
from kiro_crew.learn import LessonStore
from kiro_crew.memory import MemoryStore
from kiro_crew.skills import SkillsLoader
from kiro_crew.vector_memory import VectorMemoryStore
from kiro_crew.vector_memory_runtime.embedding import _RecallSpaceChanged

FIRST_MESSAGE = "orchid deployment"
# Shares no word with the first message; only its vector is close to it.
SEMANTIC = "Verify the flower rollout gates before shipping"
# Shares a word with the first message; its vector points elsewhere.
LEXICAL = "Never discard unrelated deployment evidence"


@pytest.fixture(autouse=True)
def _close_skills_loaders(close_skills_loaders):
    """``build_first_turn`` builds a ``ContextBuilder``: close its ``SkillsLoader`` (``test/conftest.py``)."""


class Embedder:
    """A deterministic stand-in: the first message and SEMANTIC share a direction."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, text: str) -> list[float]:
        self.calls.append(text)
        if text in (FIRST_MESSAGE, SEMANTIC):
            return [1.0, 0.0, 0.0]
        if text == LEXICAL:
            return [0.0, 1.0, 0.0]
        return [0.0, 0.0, 1.0]


@pytest.fixture
def store(tmp_path: Path):
    memory = VectorMemoryStore(db_path=tmp_path / "memory.db")
    memory.init()
    embedder = Embedder()
    memory.embed_fn = embedder
    memory.write_lesson(LEXICAL)
    memory.write_lesson(SEMANTIC)
    embedder.calls.clear()
    yield memory
    memory.close()


def build_first_turn(
    store: VectorMemoryStore, tmp_path: Path, memory: MemoryStore | None = None, **kwargs
) -> str:
    """Render a fresh session's first message against *store* as the V1 vector store.

    Without *memory* the facade is a mock whose activity block renders nothing,
    so the lessons block is the only reader of the vector.
    """
    facade: MemoryStore | MagicMock
    if memory is None:
        facade = MagicMock()
        facade._memory_version = 1
        facade.vector_store = store
        facade.get_context.return_value = ""
        facade.activity_index.return_value = ""
        facade.get_activity_context.return_value = ""
    else:
        facade = memory
    builder = ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "workspace"),
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        lessons=LessonStore(base_dir=tmp_path),
    )
    builder.get_memory_for = lambda *_args, **_kwargs: facade  # type: ignore[method-assign]
    rendered, _ = builder.build_message(FIRST_MESSAGE, True, **kwargs)
    return rendered


class TestStartupUsesTheVector:
    def test_a_rule_close_only_in_meaning_outranks_a_word_match(self, store, tmp_path) -> None:
        rendered = build_first_turn(store, tmp_path)

        assert rendered.index(SEMANTIC) < rendered.index(LEXICAL)

    def test_the_first_message_is_embedded_once(self, store, tmp_path) -> None:
        build_first_turn(store, tmp_path)

        assert store.embed_fn.calls.count(FIRST_MESSAGE) == 1

    def test_a_render_repeated_for_the_ceiling_does_not_embed_again(
        self, store, tmp_path, monkeypatch, caplog
    ) -> None:
        """Past the protected ceiling the lessons block renders twice; one embed serves both."""
        monkeypatch.setattr(context_budget, "_PROTECTED_CONTEXT_FLOOR", 300)
        for index in range(12):
            store.write_lesson(f"Keep release checklist item {index} signed by the owner")
        store.embed_fn.calls.clear()

        with caplog.at_level(logging.WARNING, logger="kiro_crew.context"):
            build_first_turn(store, tmp_path, model_window=200)

        assert "trimming lessons first" in caplog.text
        assert store.embed_fn.calls.count(FIRST_MESSAGE) == 1

    def test_one_predicate_gates_the_activity_ranking_and_the_lessons_embed(
        self, store, tmp_path, monkeypatch
    ) -> None:
        """``ranks_activity_against`` is the single gate both sites read.

        Patched to False on a store that has a vector store and a non-empty
        first message, the activity block ranks nothing and the lessons block
        never asks for the vector, so a skip added to the predicate cannot leave
        one site embedding on its own.
        """
        memory = MemoryStore(workspace=tmp_path / "workspace")
        memory.vector_store = store
        monkeypatch.setattr(memory, "ranks_activity_against", lambda query: False)
        semantic_ranking = MagicMock(wraps=store.get_semantic_context)
        monkeypatch.setattr(store, "get_semantic_context", semantic_ranking)
        lesson_query = MagicMock(wraps=store.startup_lesson_query)
        monkeypatch.setattr(store, "startup_lesson_query", lesson_query)

        build_first_turn(store, tmp_path, memory=memory)

        semantic_ranking.assert_not_called()
        lesson_query.assert_not_called()
        assert FIRST_MESSAGE not in store.embed_fn.calls


class TestAMissingOrStaleVectorRanksLexically:
    def render(self, store: VectorMemoryStore, recall_query) -> str:
        return store.get_lessons_context(
            FIRST_MESSAGE,
            background=True,
            hard_cap=99_000,
            directive_budget=7_000,
            experience_budget=1_500,
            recall_query=recall_query,
        )

    def test_no_embedder_ranks_lexically_and_never_loads_one(self, store) -> None:
        """A missing embedder ranks lexically without loading one from its factory."""
        embedder = store.embed_fn
        factory = MagicMock(return_value=embedder)
        store.embed_fn = None
        store.embed_fn_factory = factory

        query = store.startup_lesson_query(FIRST_MESSAGE)

        assert query.vector is None
        block = self.render(store, query)
        assert block.index(LEXICAL) < block.index(SEMANTIC)
        factory.assert_not_called()

    def test_an_empty_first_message_embeds_nothing(self, store) -> None:
        assert store.startup_lesson_query("   ").vector is None
        assert store.embed_fn.calls == []

    def test_a_store_read_failure_degrades_instead_of_raising(self, store, monkeypatch) -> None:
        def broken() -> None:
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(store, "recorded_embedding_space", broken)

        assert store.startup_lesson_query(FIRST_MESSAGE).vector is None

    def test_a_space_change_after_the_embed_ranks_lexically(self, store) -> None:
        query = store.startup_lesson_query(FIRST_MESSAGE)
        assert query.vector is not None
        store._space_generation += 1

        block = self.render(store, query)

        assert block.index(LEXICAL) < block.index(SEMANTIC)

    def test_explicit_recall_still_raises_on_a_space_change(self, store) -> None:
        """Only startup downgrades in place; ``recall`` owns its own keyword retry."""
        query = store.startup_lesson_query(FIRST_MESSAGE)
        store._space_generation += 1

        with pytest.raises(_RecallSpaceChanged):
            store.get_lessons_context(FIRST_MESSAGE, recall_query=query)


class TestOneScaleWithAVector:
    def test_a_dissimilar_row_does_not_keep_its_unweighted_keyword_score(self, tmp_path) -> None:
        """With a query vector, a row at cosine <= 0 scores 0.4 x keyword, not 1.0 x keyword.

        Otherwise it outranks a row the vector favours: here the favoured row
        scores 0.6 x 0.1 + 0.4 x 0.8 = 0.38 and the dissimilar one would keep 0.5.
        """
        query = "cat dog fish bird tree rock lamp desk"
        favoured = "cat dog fish bird tree rock lamp desk alphaq"
        dissimilar = "cat dog fish bird tree betaq plum pear kiwi fig lime"

        def embed(text: str) -> list[float]:
            # Distinct directions for the two rows, so the vector dedup keeps
            # both, with the stated cosine to the query each.
            if "alphaq" in text:
                return [0.1, (1 - 0.1**2) ** 0.5, 0.0]
            if "betaq" in text:
                return [-0.05, 0.0, (1 - 0.05**2) ** 0.5]
            return [1.0, 0.0, 0.0]

        memory = VectorMemoryStore(db_path=tmp_path / "scale.db")
        memory.init()
        try:
            memory.embed_fn = embed
            memory.write_lesson(favoured)
            memory.write_lesson(dissimilar)
            assert len(memory.get_lessons()) == 2, "dedup merged fixture rows"

            block = memory.get_lessons_context(
                query,
                background=True,
                hard_cap=99_000,
                directive_budget=7_000,
                experience_budget=1_500,
                recall_query=memory.startup_lesson_query(query),
            )

            assert block.index(favoured) < block.index(dissimilar)
        finally:
            memory.close()

    def test_a_negative_cosine_counts_as_no_similarity(self, tmp_path) -> None:
        """A cosine below 0 is clamped to 0, so it cannot sink a row that shares a word.

        Unclamped, the opposed row would score 0.6 x -0.9 plus its keyword term,
        below the unrelated row's 0.0, and the newer unrelated row would come first.
        """
        query = "cat dog fish"
        opposed = "cat alphaz plum pear kiwi"
        unrelated = "gamma delta epsilon zeta theta"

        def embed(text: str) -> list[float]:
            if "alphaz" in text:
                return [-0.9, (1 - 0.9**2) ** 0.5, 0.0]
            if "gamma" in text:
                return [0.0, 0.0, 1.0]
            return [1.0, 0.0, 0.0]

        memory = VectorMemoryStore(db_path=tmp_path / "clamp.db")
        memory.init()
        try:
            memory.embed_fn = embed
            memory.write_lesson(opposed)
            memory.write_lesson(unrelated)
            assert len(memory.get_lessons()) == 2, "dedup merged fixture rows"

            block = memory.get_lessons_context(
                query,
                background=True,
                hard_cap=99_000,
                directive_budget=7_000,
                experience_budget=1_500,
                recall_query=memory.startup_lesson_query(query),
            )

            assert block.index(opposed) < block.index(unrelated)
        finally:
            memory.close()


class TestUnembeddedRowsDoNotFallBackToRecency:
    def test_a_rare_word_breaks_a_saturated_keyword_tie(self, tmp_path) -> None:
        """Rows with no stored vector sharing ten words each are ordered by rarity, not recency.

        A mixed store: one row carries a vector at cosine 0.5 to the query's,
        so the ranking takes the vector branch, and the other rows were
        written while no embedder was bound, so each has no stored vector and
        scores 0.4 x ``_keyword_score(overlap)``. Every unembedded row shares
        ten words with the query, a count at which ``_keyword_score``
        saturates, so all three tie at 0.4; the query's one rare word names
        the oldest of them, and only the tie-break on the rarity-weighted
        lexical score tells it apart from the two newer rows. Fails without
        that tie-break: the stable sort would read the tied rows newest-first.
        The embedded row shares no word with the query and scores
        0.6 x 0.5 = 0.3, below the tied rows.
        """
        common = "cat dog fish bird tree rock lamp desk moon star"
        oldest = common + " quartz basalt granite marble slate shale flint pumice obsidian gneiss"
        middle = common + " violin cello oboe flute harp banjo drum tuba organ piano zither"
        newest = common + " oak elm ash birch pine cedar maple willow poplar spruce fir"
        embedded = "Sign the harbour ledger before the ferry sails"

        memory = VectorMemoryStore(db_path=tmp_path / "unembedded.db")
        memory.init()
        try:
            # Written first, while an embedder is bound: a later write with an
            # embedder bound would lazily backfill the rows that must stay
            # unembedded.
            memory.embed_fn = lambda text: [0.5, 0.0, (1 - 0.5**2) ** 0.5]
            assert memory.write_lesson(embedded)
            memory.embed_fn = None
            for text in (oldest, middle, newest):
                assert memory.write_lesson(text)
            rows = memory.get_lessons()
            assert len(rows) == 4, "dedup merged fixture rows"
            assert sum(row["embedding"] is not None for row in rows) == 1
            assert all(
                row["embedding"] is None for row in rows if embedded not in row["value_json"]
            )

            memory.embed_fn = lambda text: [1.0, 0.0, 0.0]
            query = common + " quartz"
            recall_query = memory.startup_lesson_query(query)
            assert recall_query.vector is not None

            block = memory.get_lessons_context(
                query,
                background=True,
                hard_cap=99_000,
                directive_budget=7_000,
                experience_budget=1_500,
                recall_query=recall_query,
            )

            assert block.index(oldest) < min(block.index(middle), block.index(newest))
        finally:
            memory.close()


class TestAVectorNoRowCanBeComparedWithRanksLexically:
    def rows(self, memory: VectorMemoryStore) -> tuple[str, str]:
        """Write the fixture while no embedder is bound, so no row carries a vector.

        The query shares one rare word with ``focused`` and four common words
        with ``incidental``. Counted as shared words the long row (four) beats
        the rare word (one); the filler rows carry the same four common words,
        so the rarity weights tell the two apart and the lexical scorer ranks
        ``focused`` first.
        """
        common = "cat dog fish bird"
        focused = "Rotate the quartz bearings every quarter in the depot"
        incidental = common + " tree rock lamp desk moon star violin cello oboe"
        fillers = (
            common + " oak elm ash pine cedar maple willow",
            common + " iron zinc tin lead copper nickel cobalt",
            common + " plum pear kiwi fig lime mango guava",
            common + " harp flute drum tuba organ piano banjo",
        )
        memory.write_lesson(focused)
        for filler in fillers:
            memory.write_lesson(filler)
        memory.write_lesson(incidental)
        rows = memory.get_lessons()
        assert len(rows) == 6, "dedup merged fixture rows"
        assert all(row["embedding"] is None for row in rows)
        return focused, incidental

    def render(self, memory: VectorMemoryStore, query: str, recall_query) -> str:
        return memory.get_lessons_context(
            query,
            background=True,
            hard_cap=99_000,
            directive_budget=7_000,
            experience_budget=1_500,
            recall_query=recall_query,
        )

    def test_a_rare_word_outranks_more_common_words_when_no_row_has_a_vector(
        self, tmp_path
    ) -> None:
        """A query vector no stored row can be compared with ranks exactly as no vector does.

        Every row has a vector term of 0.0, so the query vector says nothing
        about any row and the ranking is the no-vector one, through the same
        code: the row carrying the request's rare word leads the long row
        sharing only common words, and the render is byte-identical. Fails if
        such a store is scored by the capped overlap count: four common words
        (0.4) would beat the rare word (0.1).
        """
        memory = VectorMemoryStore(db_path=tmp_path / "novectors.db")
        memory.init()
        try:
            focused, incidental = self.rows(memory)
            query = "cat dog fish bird quartz"
            without_vector = self.render(memory, query, None)
            assert without_vector.index(focused) < without_vector.index(incidental)

            memory.embed_fn = lambda text: [1.0, 0.0, 0.0]
            recall_query = memory.startup_lesson_query(query)
            assert recall_query.vector is not None

            with_vector = self.render(memory, query, recall_query)

            assert with_vector.index(focused) < with_vector.index(incidental)
            assert with_vector == without_vector
        finally:
            memory.close()


class TestAVectorlessRowIsNotScaledAgainstTheBestRow:
    def test_the_best_of_a_weak_set_does_not_outrank_a_row_the_vector_favours(
        self, tmp_path
    ) -> None:
        """A vectorless row's keyword half is the capped overlap count, not a share of the best row.

        The query names a codename, ``quartz``, that one vectorless row shares
        as its only word in common; no other row shares any word, so that row
        is the best lexical row in the store. The embedded row shares no word
        with the query and its vector sits at cosine 0.5, scoring
        0.6 x 0.5 = 0.3. The vectorless row scores 0.4 x 0.1 = 0.04, so the
        embedded row leads. Fails if a vectorless row's keyword half is
        normalised against the best row among those ranked: this row would
        take 1.0 as its keyword half, score 0.4 and displace the on-topic
        embedded row.
        """
        embedded = "Page payments oncall when cart purchases fail"
        codename = "Rotate quartz bearings every quarter"
        fillers = (
            "Keep harbour ledger signed before ferry sails",
            "Stack oak elm ash pine cedar maple planks",
            "Store iron zinc tin lead copper nickel",
        )

        memory = VectorMemoryStore(db_path=tmp_path / "scale.db")
        memory.init()
        try:
            # Written first, while an embedder is bound: a later write with an
            # embedder bound would lazily backfill the rows that must stay
            # unembedded.
            memory.embed_fn = lambda text: [0.5, 0.0, (1 - 0.5**2) ** 0.5]
            assert memory.write_lesson(embedded)
            memory.embed_fn = None
            for text in (codename, *fillers):
                assert memory.write_lesson(text)
            rows = memory.get_lessons()
            assert len(rows) == 5, "dedup merged fixture rows"
            assert [
                embedded in row["value_json"] for row in rows if row["embedding"] is not None
            ] == [True]

            memory.embed_fn = lambda text: [1.0, 0.0, 0.0]
            query = "investigate checkout outage quartz incident codename"
            recall_query = memory.startup_lesson_query(query)
            assert recall_query.vector is not None

            block = memory.get_lessons_context(
                query,
                background=True,
                hard_cap=99_000,
                directive_budget=7_000,
                experience_budget=1_500,
                recall_query=recall_query,
            )

            assert block.index(embedded) < block.index(codename)
        finally:
            memory.close()


class TestGainingAVectorNeverLowersARow:
    def test_a_row_scores_no_lower_at_a_small_positive_cosine_than_with_no_vector(
        self, tmp_path
    ) -> None:
        """Every row takes the same keyword half, so a positive cosine can only add to its score.

        Twenty rows are ranked. Two three-word rows each share the request's
        one rare word, ``quartz``: one has no stored vector, the other is
        embedded at cosine 0.05. A longer row sharing that word sits at
        cosine 0.30. Every row sharing the word takes
        ``_keyword_score(1) = 0.1`` as its keyword half, so the no-vector row
        scores 0.04, its embedded twin 0.6 x 0.05 + 0.04 = 0.07, and the
        cosine-0.30 row 0.18 + 0.04 = 0.22. Fails if a row the vector says
        nothing about takes a different keyword half: on the rarity scale the
        no-vector row scores 0.4 x (ln 6 / sqrt 3) / ln 14 = 0.157 and is
        ranked above its embedded twin, so raising a row's cosine from 0 to
        0.05 lowers it.
        """
        no_vector = "Rotate quartz bearings"
        # Shares only ``quartz`` with ``no_vector``: two shared significant words
        # out of three would let the writer's topic-overlap dedup merge them.
        embedded_twin = "Grease quartz gears"
        favoured = "Log every quartz reading in the depot ledger"
        fillers = (
            "Stack oak elm ash planks",
            "Store iron zinc tin copper",
            "Keep plum pear kiwi crates",
            "Tune harp flute drum tuba",
            "Sign harbour ferry manifests early",
            "Count sedan coupe wagon keys",
            "Paint violet amber crimson walls",
            "Mend wool linen silk hems",
            "Check basalt granite marble slabs",
            "Weigh barley oats millet sacks",
            "Label falcon heron sparrow cages",
            "Fold maple birch cedar maps",
            "Rinse cobalt nickel pewter bowls",
            "Sort ruby topaz garnet trays",
            "Dust piano organ banjo cases",
            "Wind clock pendulum spring coils",
            "Bind velvet cotton denim rolls",
        )

        def embed(text: str) -> list[float]:
            if "gears" in text and "quartz" in text:
                return [0.05, (1 - 0.05**2) ** 0.5, 0.0]
            if "ledger" in text:
                return [0.30, 0.0, (1 - 0.30**2) ** 0.5]
            return [1.0, 0.0, 0.0]

        memory = VectorMemoryStore(db_path=tmp_path / "gain.db")
        memory.init()
        try:
            # Written first, while an embedder is bound: a later write with an
            # embedder bound would lazily backfill the rows that must stay
            # unembedded.
            memory.embed_fn = embed
            assert memory.write_lesson(embedded_twin)
            assert memory.write_lesson(favoured)
            memory.embed_fn = None
            for text in (no_vector, *fillers):
                assert memory.write_lesson(text)
            rows = memory.get_lessons()
            assert len(rows) == 20, "dedup merged fixture rows"
            embedded_rows = [row["value_json"] for row in rows if row["embedding"] is not None]
            assert len(embedded_rows) == 2
            assert all(
                any(text in body for body in embedded_rows) for text in (embedded_twin, favoured)
            )

            memory.embed_fn = embed
            query = "calibrate quartz sensor"
            recall_query = memory.startup_lesson_query(query)
            assert recall_query.vector == [1.0, 0.0, 0.0]

            block = memory.get_lessons_context(
                query,
                background=True,
                hard_cap=99_000,
                directive_budget=7_000,
                experience_budget=1_500,
                recall_query=recall_query,
            )

            assert block.index(embedded_twin) < block.index(no_vector)
            assert block.index(favoured) < block.index(no_vector)
        finally:
            memory.close()
