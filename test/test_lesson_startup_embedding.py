"""Startup lessons rank with the vector the activity block already computed.

On a V1 session whose activity block embeds the first message, the prompt
builder reuses that vector (``startup_lesson_query``, served from the shared
embed cache) for every render of the lessons block. These cases pin that the
vector reaches the ranking, that a render repeated to fit the protected ceiling
does not embed again, that every row is scored on one weighted scale once there
is a vector, that every row takes the same keyword half (the rarity-weighted
overlap, on a scale fixed by the number of rows ranked and their median length)
with its cosine clamped at 0 as the vector term, so a row the vector
says nothing about (no comparable stored vector, or a cosine at or below 0)
is not measured against the best row in the set, a positive cosine only raises
a row within one ranking, the rows a vector cannot rank keep the order they
have with no vector, explicit recall on a fully embedded store separates rows
the overlap count ties, one rare word in a row of the store's median length
is worth the same share of the keyword half in a store of short rules as in a
store of long ones, so one shared function word in a short rule cannot outrank
a rule the vector favours, the keyword half stays bounded so a
row saturating it cannot outrank a row the vector favours more, rows tied ON
that bound are ordered by word rarity rather than recency, and a store in which
no row has a positive cosine ranks exactly as
no vector does, and that every way the vector can be missing or stale
ranks lexically instead of failing the build, a missing embedder is never
loaded from its factory, and a session that excludes the memory group keeps its
lessons and embeds nothing. ``test_memory_v1_golden`` pins the inference count and
that ``inject_activity: false`` still embeds nothing.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kiro_crew import memory_schema, vector_memory
from kiro_crew._sqlite_compat import sqlite3
from kiro_crew.config import loader as loader_mod
from kiro_crew.config.loader import config_dir
from kiro_crew.context import (
    CONTEXT_GROUP_MEMORY,
    SWITCHABLE_CONTEXT_GROUPS,
    ContextBuilder,
)
from kiro_crew.context_assembly import budget as context_budget
from kiro_crew.learn import LessonStore
from kiro_crew.memory import MemoryStore
from kiro_crew.memory_stores import DEFAULT_MEMORY_STORE, resolve_store_path
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


def _stamp_lessons(memory: VectorMemoryStore, stamps: dict[str, str]) -> None:
    """Set the ``updated_at`` of the lesson whose rule is exactly each text in *stamps*.

    The row is found by its exact rule and written by its exact ``key``, so a
    fixture text that is a substring of another lesson stamps only its own row.
    The write goes to the lineage's writable relation: on a crew store
    ``semantic_memory`` is a read-only view over ``memory_items``.
    """
    with memory._db_lock, memory.db:
        rows = memory.db.execute(
            "SELECT key, value_json FROM semantic_memory "
            "WHERE is_deleted = 0 AND key LIKE 'lesson.%'"
        ).fetchall()
        for text, stamp in stamps.items():
            keys = [row["key"] for row in rows if json.loads(row["value_json"])["rule"] == text]
            assert len(keys) == 1, f"fixture text matched {len(keys)} lessons: {text!r}"
            changed = memory.db.execute(
                f"UPDATE {memory._sem_rel} SET updated_at = ? WHERE key = ?",
                (stamp, keys[0]),
            ).rowcount
            assert changed == 1, f"stamp wrote {changed} rows for {text!r}"


def _pin_write_order(memory: VectorMemoryStore, texts) -> None:
    """Stamp the lessons holding *texts* one second apart, oldest first.

    The startup order is newest-first by ``updated_at``. Two writes inside one
    clock tick (routine on Windows) share a stamp, and the tie then falls to
    ``key``, which says nothing about which row is newer, so a test that relies
    on recency pins that order here instead of on the clock.
    """
    _stamp_lessons(
        memory, {text: f"2026-01-01T00:00:{index:02d}+00:00" for index, text in enumerate(texts)}
    )


def _stamp_one_tick(memory: VectorMemoryStore, texts) -> None:
    """Give every lesson holding *texts* one shared ``updated_at``, as one clock tick would."""
    _stamp_lessons(memory, dict.fromkeys(texts, "2026-01-01T00:00:00+00:00"))


def _distinct_rows(count: int, words: int, tag: str) -> tuple[str, ...]:
    """*count* rules of exactly *words* distinct words that share none but ``Stack``.

    Every other word carries the row's *tag* and index, so no request word reaches
    these rows and the writer's dedup keeps each of them. The row length is what a
    fixture sets with them: the keyword half is scaled against the median length
    of the rows ranked.
    """
    return tuple(
        "Stack " + " ".join(f"{tag}{index}w{word}" for word in range(words - 1))
        for index in range(count)
    )


def _open_lineage_store(tmp_path: Path, lineage: str) -> VectorMemoryStore:
    """Open an initialized store of *lineage* (``v1`` file or a declared crew silo).

    A crew silo is the file a declared named store resolves to, which is what
    makes ``init`` build the ``memory_items`` table with ``semantic_memory`` as
    a read-only view over it.
    """
    if lineage == memory_schema.LINEAGE_V1:
        db_path = tmp_path / "one-tick.db"
    else:
        payload = {
            "memory_stores": {DEFAULT_MEMORY_STORE: {}, "ledger": {}},
            "default_memory_store": DEFAULT_MEMORY_STORE,
        }
        (config_dir() / "config.json").write_text(json.dumps(payload), encoding="utf-8")
        loader_mod._invalidate_config_cache()
        db_path = resolve_store_path("ledger")
        db_path.parent.mkdir(parents=True, exist_ok=True)
    memory = VectorMemoryStore(db_path=db_path)
    memory.init()
    assert memory._lineage == lineage
    return memory


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


@pytest.fixture
def distinct_write_instants(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stamp each write later than the one before, so the last row written is the newest.

    ``updated_at`` comes from ``vector_memory._now_iso``, which reads the wall
    clock, and that ticks about every 15.6 ms on Windows under Python 3.12, so two
    rows written back to back can carry one stamp. ``get_lessons`` orders such a
    pair by ``key``, which is stable but says nothing about which row is newer.
    A case whose assertion rests on recency takes its stamps from here.
    """
    ticks = iter(f"2026-01-01T00:00:00.{tick:06d}+00:00" for tick in range(1, 1000))
    monkeypatch.setattr(vector_memory, "_now_iso", lambda: next(ticks))


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

    def test_a_session_that_excludes_memory_keeps_its_lessons_and_embeds_nothing(
        self, store, tmp_path
    ) -> None:
        """A build can keep lessons and drop memory, and then no site ranks against a vector.

        ``activity_ranked`` is set inside the memory group's own branch, so a
        subagent spawned with ``include_memory=false, include_lessons=true``
        never reaches the predicate above: its lessons block still renders, from
        the same store, ranked lexically and embedding nothing.
        """
        rendered = build_first_turn(
            store,
            tmp_path,
            context_groups=frozenset(SWITCHABLE_CONTEXT_GROUPS) - {CONTEXT_GROUP_MEMORY},
        )

        assert rendered.index(LEXICAL) < rendered.index(SEMANTIC)
        assert store.embed_fn.calls == []


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

        Otherwise it outranks a row the vector favours. The request's four rare
        words name one row, whose vector points away from it (cosine clamped
        to 0); ten rows carry the request's two common words, one of them at
        cosine 0.5. On the 0.6/0.4 scale the rare row takes keyword 0.365 and
        scores 0.146, below the favoured row's 0.6 x 0.5 + 0.4 x 0.013 =
        0.305. Keeping its keyword unweighted the rare row would score 0.365
        and lead.
        """
        query = "calibrate rotate quartz every quarter cat dog"
        dissimilar = "Rotate the quartz bearings every quarter"
        favoured = "Keep cat dog crates stacked"
        fillers = (
            "Walk cat dog routes daily",
            "Store cat dog iron zinc",
            "Stack cat dog oak elm",
            "Tune cat dog harp flute",
            "Weigh cat dog barley oats",
            "Label cat dog falcon heron",
            "Rinse cat dog cobalt nickel",
            "Sort cat dog ruby topaz",
            "Dust cat dog piano organ",
        )

        def embed(text: str) -> list[float]:
            # Keyed on words the REQUEST does not carry, so the request itself
            # still embeds to [1, 0, 0] and each row keeps its stated cosine.
            if "bearings" in text:
                return [-0.05, 0.0, (1 - 0.05**2) ** 0.5]
            if "crates" in text:
                return [0.5, (1 - 0.5**2) ** 0.5, 0.0]
            return [1.0, 0.0, 0.0]

        memory = VectorMemoryStore(db_path=tmp_path / "scale.db")
        memory.init()
        try:
            # Written first, while an embedder is bound: a later write with an
            # embedder bound would lazily backfill the rows that must stay
            # unembedded.
            memory.embed_fn = embed
            assert memory.write_lesson(dissimilar)
            assert memory.write_lesson(favoured)
            memory.embed_fn = None
            for text in fillers:
                assert memory.write_lesson(text)
            rows = memory.get_lessons()
            assert len(rows) == 11, "dedup merged fixture rows"
            assert sum(row["embedding"] is not None for row in rows) == 2

            memory.embed_fn = embed
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
    def test_rows_the_vector_cannot_rank_are_ordered_by_rarity(self, tmp_path) -> None:
        """Rows with no stored vector sharing ten words each are ordered by rarity, not recency.

        A mixed store: one row carries a vector at cosine 0.5 to the query's,
        so the ranking takes the vector branch, and the other rows were
        written while no embedder was bound, so each has no stored vector and
        scores 0.4 x its keyword half. Every unembedded row shares the same
        ten common words with the query; the query's one rare word names the
        oldest of them, which lifts its keyword half to 0.401 against the two
        newer rows' 0.293, so it scores 0.160 against their 0.117. The
        embedded row shares no word with the query and scores 0.6 x 0.5 = 0.3,
        above all three.

        This case passes under the capped overlap count too, which saturates
        on these rows and leaves the tie-break to separate them, so it pins
        the PRIMARY score's rarity ordering rather than the choice of measure:
        ``TestOneKeywordMeasureAcrossTheVectorBoundary`` pins the measure and
        ``TestTheKeywordHalfStaysBounded`` the cap and the tie-break.
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
        """A vectorless row's keyword half is on a fixed scale, not a share of the best row.

        The query names a codename, ``quartz``, that one vectorless row shares
        as its only word in common; no other row shares any word, so that row
        is the best lexical row in the store. The embedded row shares no word
        with the query and its vector sits at cosine 0.5, scoring
        0.6 x 0.5 = 0.3. The vectorless row's rarity half is its lexical score
        times the store's keyword scale (0.1 x sqrt 7 over the weight of a word
        only one of the five rows carries), 0.620 x 0.191 = 0.118, so it scores
        0.4 x 0.118 = 0.047 and the embedded row leads. Fails if a vectorless
        row's keyword half is normalised against the best row among those
        ranked: this row would take 1.0 as its keyword half, score 0.4 and
        displace the on-topic embedded row.
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

        Twenty rows are ranked, seventeen of them 25-word fillers, so the median
        row is 25 words long. Two three-word rows each share the request's one
        rare word, ``quartz``: one has no stored vector, the other is embedded
        at cosine 0.05. A longer row sharing that word sits at cosine 0.30. The
        two three-word rows take the same keyword half, 0.196, so the no-vector
        row scores 0.078, its embedded twin 0.6 x 0.05 + 0.078 = 0.108, and the
        longer cosine-0.30 row, whose extra words dilute the same shared weight
        to 0.120, scores 0.228. Fails if a row the vector says nothing about
        takes a DIFFERENT keyword half from its embedded siblings: scoring
        vectorless rows by rarity while embedded rows keep the capped overlap
        count puts the no-vector row at 0.078 and its embedded twin at 0.070,
        so raising a row's cosine from 0 to 0.05 lowers it.
        """
        no_vector = "Rotate quartz bearings"
        # Shares only ``quartz`` with ``no_vector``: two shared significant words
        # out of three would let the writer's topic-overlap dedup merge them.
        embedded_twin = "Grease quartz gears"
        favoured = "Log every quartz reading in the depot ledger"
        # Long fillers keep the median row long, so a three-word row's one rare
        # word takes a larger share of the keyword half than the count's 0.1.
        fillers = _distinct_rows(17, 25, "g")

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


class TestOneKeywordMeasureAcrossTheVectorBoundary:
    """The rare word and the two common words are the whole fixture.

    ``cat`` and ``dog`` are carried by ten of the eleven rows, so their rarity
    weight is near nothing; ``quartz`` is carried by one. Counting shared words
    instead, the two common words (0.2) outscore the one rare word (0.1), which
    is what these cases rule out.
    """

    RARE = "Rotate the quartz bearings every quarter"
    COMMON_A = "Keep cat dog crates stacked"
    COMMON_B = "Walk cat dog routes daily"
    FILLERS = (
        "Store cat dog iron zinc",
        "Stack cat dog oak elm",
        "Tune cat dog harp flute",
        "Weigh cat dog barley oats",
        "Label cat dog falcon heron",
        "Rinse cat dog cobalt nickel",
        "Sort cat dog ruby topaz",
        "Dust cat dog piano organ",
    )
    QUERY = "calibrate quartz cat dog"

    def render(self, memory: VectorMemoryStore, recall_query=None) -> str:
        return memory.get_lessons_context(
            self.QUERY,
            background=True,
            hard_cap=99_000,
            directive_budget=7_000,
            experience_budget=1_500,
            recall_query=recall_query,
        )

    @pytest.mark.usefixtures("distinct_write_instants")
    def test_the_first_embedded_row_does_not_reshuffle_the_rows_behind_it(self, tmp_path) -> None:
        """One row gaining a vector leaves the order of the rows it cannot rank unchanged.

        Only one filler carries a vector, and none of the three compared rows
        does, so each of them keeps a vector term of 0 and the keyword half
        alone separates them. The rare row's half is 0.091 against the common
        rows' 0.013, so it scores 0.037 against their 0.005. The same store is
        rendered twice, once with no query vector and once with one, and the
        three rows keep their order. The two common rows tie on every score, so
        recency alone puts the newer ``COMMON_B`` first, which is why the writes
        take distinct stamps.

        Fails if the keyword half with a vector is the capped overlap count:
        one rare word scores 0.1 and two common words 0.2, so the rare row
        drops behind both common rows (0.04 against 0.08) the moment the first
        filler is embedded, though it leads with no vector at all.
        """
        memory = VectorMemoryStore(db_path=tmp_path / "boundary.db")
        memory.init()
        try:
            # Written first, while an embedder is bound: a later write with an
            # embedder bound would lazily backfill the rows that must stay
            # unembedded.
            memory.embed_fn = lambda text: [0.4, 0.0, (1 - 0.4**2) ** 0.5]
            assert memory.write_lesson(self.FILLERS[0])
            memory.embed_fn = None
            for text in (self.RARE, self.COMMON_A, self.COMMON_B, *self.FILLERS[1:]):
                assert memory.write_lesson(text)
            _pin_write_order(
                memory,
                (self.FILLERS[0], self.RARE, self.COMMON_A, self.COMMON_B, *self.FILLERS[1:]),
            )
            rows = memory.get_lessons()
            assert len(rows) == 11, "dedup merged fixture rows"
            assert sum(row["embedding"] is not None for row in rows) == 1

            lexical_block = self.render(memory)
            memory.embed_fn = lambda text: [1.0, 0.0, 0.0]
            recall_query = memory.startup_lesson_query(self.QUERY)
            assert recall_query.vector is not None
            hybrid_block = self.render(memory, recall_query)

            def order(block: str) -> list[str]:
                return sorted((self.RARE, self.COMMON_A, self.COMMON_B), key=block.index)

            assert order(lexical_block) == [self.RARE, self.COMMON_B, self.COMMON_A]
            assert order(hybrid_block) == order(lexical_block)
        finally:
            memory.close()

    def test_rows_written_inside_one_clock_tick_keep_one_order_across_the_boundary(
        self, tmp_path
    ) -> None:
        """Every lesson shares one ``updated_at``: the common pair still has one order.

        ``COMMON_A`` and ``COMMON_B`` tie on the hybrid score and on the
        lexical score, so ``rank_lessons`` keeps the order ``get_lessons``
        hands it. With every row on one stamp, as two writes inside one tick
        of a coarse clock leave them, that order is ``key`` order, so the pair
        renders in key order behind the rare row, with and without a query
        vector.

        Fails if the ``key`` tie-break is dropped from ``get_lessons``: SQLite
        then returns the tied rows in insertion order, ``COMMON_A`` before
        ``COMMON_B``, which is the reverse of their key order here.
        """
        memory = VectorMemoryStore(db_path=tmp_path / "boundary-one-tick.db")
        memory.init()
        try:
            memory.embed_fn = lambda text: [0.4, 0.0, (1 - 0.4**2) ** 0.5]
            assert memory.write_lesson(self.FILLERS[0])
            memory.embed_fn = None
            written = (self.RARE, self.COMMON_A, self.COMMON_B, *self.FILLERS[1:])
            for text in written:
                assert memory.write_lesson(text)
            _stamp_one_tick(memory, (self.FILLERS[0], *written))
            rows = memory.get_lessons()
            assert len(rows) == 11, "dedup merged fixture rows"
            assert sum(row["embedding"] is not None for row in rows) == 1

            def key_of(text: str) -> str:
                return next(row["key"] for row in rows if text in row["value_json"])

            by_key = sorted((self.COMMON_A, self.COMMON_B), key=key_of)
            # Written A then B: the scan order cannot satisfy key order by accident.
            assert by_key == [self.COMMON_B, self.COMMON_A]

            lexical_block = self.render(memory)
            memory.embed_fn = lambda text: [1.0, 0.0, 0.0]
            recall_query = memory.startup_lesson_query(self.QUERY)
            assert recall_query.vector is not None
            hybrid_block = self.render(memory, recall_query)

            def order(block: str) -> list[str]:
                return sorted((self.RARE, self.COMMON_A, self.COMMON_B), key=block.index)

            assert order(lexical_block) == [self.RARE, *by_key]
            assert order(hybrid_block) == order(lexical_block)
        finally:
            memory.close()

    def test_explicit_recall_separates_rows_the_count_ties(self, tmp_path) -> None:
        """On a fully embedded store at one cosine, the rarer shared word decides.

        Every row sits at cosine 0.5 to the request and is mutually dissimilar
        (each carries its own dimension), so the vector term is equal for all
        of them and only the keyword half orders the set. The rare row scores
        0.3 + 0.4 x 0.091 = 0.337 against the common rows' 0.3 + 0.4 x 0.013 =
        0.305. This is the path ``memory_recall`` takes on a store whose
        backfill has finished.

        Fails if the keyword half is the capped overlap count: the rare row
        then scores 0.34 against the common rows' 0.38, so both outrank it and
        recall reports them as the better match.
        """
        texts = (self.RARE, self.COMMON_A, self.COMMON_B, *self.FILLERS)
        width = 1 + len(texts)
        vectors = {}
        for index, text in enumerate(texts):
            vector = [0.0] * width
            vector[0] = 0.5
            vector[1 + index] = (1 - 0.5**2) ** 0.5
            vectors[text] = vector
        query_vector = [1.0] + [0.0] * (width - 1)

        memory = VectorMemoryStore(db_path=tmp_path / "embedded.db")
        memory.init()
        try:
            memory.embed_fn = lambda text: vectors.get(text, query_vector)
            for text in texts:
                assert memory.write_lesson(text)
            rows = memory.get_lessons()
            assert len(rows) == 11, "dedup merged fixture rows"
            assert all(row["embedding"] is not None for row in rows)

            block = self.render(memory, memory.startup_lesson_query(self.QUERY))

            assert block.index(self.RARE) < block.index(self.COMMON_A)
            assert block.index(self.RARE) < block.index(self.COMMON_B)
        finally:
            memory.close()


class TestTheKeywordHalfStaysBounded:
    """Three lines the scaled keyword half rests on, each pinned by a mutation.

    The rarity-weighted overlap is scaled so one maximally rare word in a row of
    the store's median length is worth 0.1 of the keyword half, and a row
    shorter than the median that shares several rare words can still fill it:
    the cap that bounds the half, the tie-break that orders rows sitting ON the
    cap and the rarity divisor in the scale are all load-bearing. None is
    covered by a case that merely ranks unsaturated rows on a small store: those
    pass with any of the three deleted. The fillers are 25 words long, so the
    median row is long enough for a short row's rare words to reach the cap.
    """

    FILLERS = _distinct_rows(6, 25, "k")

    def render(self, memory: VectorMemoryStore, query: str, recall_query) -> str:
        return memory.get_lessons_context(
            query,
            background=True,
            hard_cap=99_000,
            directive_budget=7_000,
            experience_budget=1_500,
            recall_query=recall_query,
        )

    def test_a_saturating_row_does_not_outrank_a_row_at_a_higher_cosine(self, tmp_path) -> None:
        """A row whose raw keyword half exceeds 1.0 is capped, so the vector still wins.

        ``saturating`` is nine words long and the request carries all nine,
        each held by no other row, so its raw half is 0.1 x sqrt 25 x 9 /
        sqrt 9 = 1.5 -- above the cap. Capped at 1.0 it scores 0.4 x 1.0 =
        0.400 with no vector of its own, and ``favoured``, which shares no word
        with the request, scores 0.6 x 0.8 = 0.480 on its cosine alone, so the
        vector's row leads.

        Fails if ``min(1.0, ...)`` is dropped from the keyword half: the
        saturating row then scores 0.4 x 1.5 = 0.600 and overtakes the row at
        cosine 0.8 that shares no word with the request, which is the whole
        point of keeping the half on [0, 1].
        """
        saturating = "quartz bearings calibrate gauge piston valve rotor spindle flange"
        favoured = "Sign the harbour ferry manifest"
        query = "quartz bearings calibrate gauge piston valve rotor spindle flange every sensor"

        memory = VectorMemoryStore(db_path=tmp_path / "cap.db")
        memory.init()
        try:
            # Written first, while an embedder is bound: a later write with an
            # embedder bound would lazily backfill the rows that must stay
            # unembedded.
            memory.embed_fn = lambda text: [0.8, 0.0, (1 - 0.8**2) ** 0.5]
            assert memory.write_lesson(favoured)
            memory.embed_fn = None
            for text in (saturating, *self.FILLERS):
                assert memory.write_lesson(text)
            rows = memory.get_lessons()
            assert len(rows) == 8, "dedup merged fixture rows"
            assert sum(row["embedding"] is not None for row in rows) == 1

            memory.embed_fn = lambda text: [1.0, 0.0, 0.0]
            recall_query = memory.startup_lesson_query(query)
            assert recall_query.vector is not None

            block = self.render(memory, query, recall_query)

            assert block.index(favoured) < block.index(saturating)
        finally:
            memory.close()

    @pytest.mark.usefixtures("distinct_write_instants")
    def test_two_rows_at_the_cap_are_ordered_by_rarity_not_recency(self, tmp_path) -> None:
        """Rows tied ON the cap at one cosine are separated by the score behind it.

        ``richer`` is six words and ``shorter`` five; the request carries every
        word of both, each held by no other row, so their raw halves are
        0.1 x sqrt 25 x sqrt 6 = 1.225 and 0.1 x sqrt 25 x sqrt 5 = 1.118 --
        both above the cap, so both are capped to 1.0.
        Neither has a stored vector, so both score exactly 0.4 and the primary
        score cannot order them. ``richer`` is written FIRST, and the writes
        take distinct stamps, so the caller's newest-first order puts
        ``shorter`` ahead of it, and only the ``-lexical`` tie-break puts the
        row with the larger rarity-weighted overlap back on top.

        Fails if ``-lexical[index]`` is dropped from the sort key: the stable
        sort then returns the tied pair newest-first and ``shorter`` leads,
        which is the recency fallback the tie-break exists to prevent.
        """
        richer = "quartz bearings calibrate depot gauge piston"
        shorter = "harbour ferry manifest dock pier"
        embedded = "Stack cobalt nickel pewter bowls"
        query = "quartz bearings calibrate depot gauge piston harbour ferry manifest dock pier"

        memory = VectorMemoryStore(db_path=tmp_path / "tie.db")
        memory.init()
        try:
            # Written first, while an embedder is bound: a later write with an
            # embedder bound would lazily backfill the rows that must stay
            # unembedded.
            memory.embed_fn = lambda text: [0.5, 0.0, (1 - 0.5**2) ** 0.5]
            assert memory.write_lesson(embedded)
            memory.embed_fn = None
            # richer first, so recency alone would rank shorter above it.
            for text in (richer, shorter, *self.FILLERS):
                assert memory.write_lesson(text)
            _pin_write_order(memory, (embedded, richer, shorter, *self.FILLERS))
            rows = memory.get_lessons()
            assert len(rows) == 9, "dedup merged fixture rows"
            assert sum(row["embedding"] is not None for row in rows) == 1
            unembedded = [row["value_json"] for row in rows if row["embedding"] is None]
            assert next(index for index, body in enumerate(unembedded) if shorter in body) < next(
                index for index, body in enumerate(unembedded) if richer in body
            ), "fixture assumes shorter is the newer of the tied pair"

            memory.embed_fn = lambda text: [1.0, 0.0, 0.0]
            recall_query = memory.startup_lesson_query(query)
            assert recall_query.vector is not None

            block = self.render(memory, query, recall_query)

            assert block.index(richer) < block.index(shorter)
        finally:
            memory.close()

    @pytest.mark.parametrize("lineage", [memory_schema.LINEAGE_V1, memory_schema.LINEAGE_CREW])
    def test_rows_written_inside_one_clock_tick_have_a_defined_order(
        self, tmp_path, lineage: str
    ) -> None:
        """Every lesson shares one ``updated_at``: the read order is still total.

        Two writes inside one tick of a coarse clock store the same stamp, which
        is routine on a Windows runner. ``get_lessons`` then orders
        the tie by ``key``, so the newest-first input ``rank_lessons`` keeps for
        its last ties, and every ``LIMIT/OFFSET`` page, rests on a defined
        order rather than on the query plan. The pair tied on the cap is still
        ordered by rarity, because the ``-lexical`` tie-break does not read
        recency at all. Runs on both lineages: the crew store reads lessons
        through the ``semantic_memory`` view, where ``rowid`` is not readable,
        which is why the tie-break is ``key``.

        Fails if the ``key`` tie-break is dropped from ``get_lessons``: SQLite
        then returns the tied rows in scan (insertion) order, which is not key
        order for this fixture.
        """
        richer = "quartz bearings calibrate depot gauge piston"
        shorter = "harbour ferry manifest dock pier"
        embedded = "Stack cobalt nickel pewter bowls"
        query = "quartz bearings calibrate depot gauge piston harbour ferry manifest dock pier"

        memory = _open_lineage_store(tmp_path, lineage)
        try:
            memory.embed_fn = lambda text: [0.5, 0.0, (1 - 0.5**2) ** 0.5]
            assert memory.write_lesson(embedded)
            memory.embed_fn = None
            written = (embedded, richer, shorter, *self.FILLERS)
            for text in written[1:]:
                assert memory.write_lesson(text)
            _stamp_one_tick(memory, written)

            rows = memory.get_lessons()
            assert len(rows) == 9, "dedup merged fixture rows"
            keys = [row["key"] for row in rows]
            assert keys == sorted(keys)
            # Insertion order differs from key order here, so the assertion
            # above is not satisfied by the scan order by accident.
            inserted = [
                next(row["key"] for row in rows if text in row["value_json"]) for text in written
            ]
            assert inserted != sorted(inserted)
            paged = [row["key"] for offset in range(9) for row in memory.get_lessons(1, offset)]
            assert paged == keys

            memory.embed_fn = lambda text: [1.0, 0.0, 0.0]
            recall_query = memory.startup_lesson_query(query)
            assert recall_query.vector is not None

            block = self.render(memory, query, recall_query)

            assert block.index(richer) < block.index(shorter)
        finally:
            memory.close()

    @pytest.mark.parametrize("row_count", [20, 80])
    def test_one_rare_word_takes_the_same_keyword_half_at_any_store_size(
        self, tmp_path, row_count: int
    ) -> None:
        """The rarity divisor keeps one rare word's share of the keyword half fixed as the store grows.

        With N rows ranked, a word one row carries weighs w1 = ln((N + 1) /
        1.5), and the keyword scale divides by it. ``single`` is four words
        long in a store whose median row has 49, and shares only ``quartz``
        with the request, carried by no other row, so its keyword half is
        0.1 x sqrt 49 x (w1 / sqrt 4) / w1 = 0.35 at every store size and it
        scores 0.4 x 0.35 = 0.14 with no vector of its own. ``favoured``
        shares no word with the request and scores 0.6 x 0.5 = 0.3 on its
        cosine alone, so it leads at both sizes.

        Fails if the scale does not divide by w1: the half becomes 0.35 x w1,
        0.924 at 20 rows and capped to 1.0 at 80, so the one-word match scores
        0.370 or 0.400 and overtakes the row at cosine 0.5.
        """
        single = "Rotate quartz bearings weekly"
        favoured = "Sign the harbour ferry manifest"
        query = "calibrate quartz sensor"
        fillers = _distinct_rows(row_count - 2, 49, "d")

        memory = VectorMemoryStore(db_path=tmp_path / "divisor.db")
        memory.init()
        try:
            # Written first, while an embedder is bound: a later write with an
            # embedder bound would lazily backfill the rows that must stay
            # unembedded.
            memory.embed_fn = lambda text: [0.5, 0.0, (1 - 0.5**2) ** 0.5]
            assert memory.write_lesson(favoured)
            memory.embed_fn = None
            for text in (single, *fillers):
                assert memory.write_lesson(text)
            rows = memory.get_lessons()
            assert len(rows) == row_count, "dedup merged fixture rows"
            assert sum(row["embedding"] is not None for row in rows) == 1

            memory.embed_fn = lambda text: [1.0, 0.0, 0.0]
            recall_query = memory.startup_lesson_query(query)
            assert recall_query.vector is not None

            block = self.render(memory, query, recall_query)

            assert block.index(favoured) < block.index(single)
        finally:
            memory.close()


class TestTheKeywordHalfIsScaledToTheMedianRow:
    """One rare word is worth the same share of the keyword half in any store.

    The overlap divides a row's rarity weight by the square root of its length.
    Scaled by the heaviest weight alone, one rare word would fill
    ``1 / sqrt(size)`` of a row's keyword half, so it would weigh more in a store
    of short rules than in one of long rules, and a function word only one or
    two short rules carry would weigh as a rare word. Length is measured
    against the median row ranked instead: one maximally rare word in a
    median-length row is worth 0.1 of the half.
    """

    def render(self, memory: VectorMemoryStore, query: str, recall_query) -> str:
        return memory.get_lessons_context(
            query,
            background=True,
            hard_cap=99_000,
            directive_budget=7_000,
            experience_budget=1_500,
            recall_query=recall_query,
        )

    def test_one_shared_function_word_does_not_outrank_a_closer_rule(self, tmp_path) -> None:
        """In a store of short rules, ``use`` in a wrong rule does not beat the right rule's cosine.

        Twelve rules of ten to thirteen words; the median has twelve. The
        request shares no word with ``right``, which sits at cosine 0.57 and
        scores 0.342. ``wrong`` sits at cosine 0.48 and shares only ``use``
        with the request, a word no other rule here carries, so it weighs as a
        rare word: its keyword half is 0.1 x sqrt(12 / 13) = 0.096 and it
        scores 0.288 + 0.038 = 0.326, below ``right``.

        Fails if row length is measured absolutely: ``wrong``'s half becomes
        1 / sqrt 13 = 0.277, it scores 0.399, and one shared function word
        outranks the rule the vector favours.
        """
        right = "Show every time to the user in Pacific Time, never in UTC."
        wrong = "Use no em dashes in prose written for people; use a full stop instead."
        query = "what clock should I use when I report something that happened"
        fillers = _distinct_rows(10, 12, "s")

        def embed(text: str) -> list[float]:
            if "Pacific" in text:
                return [0.57, (1 - 0.57**2) ** 0.5, 0.0]
            if "dashes" in text:
                return [0.48, 0.0, (1 - 0.48**2) ** 0.5]
            return [1.0, 0.0, 0.0]

        memory = VectorMemoryStore(db_path=tmp_path / "short-rules.db")
        memory.init()
        try:
            # Written first, while an embedder is bound: a later write with an
            # embedder bound would lazily backfill the rows that must stay
            # unembedded.
            memory.embed_fn = embed
            assert memory.write_lesson(right)
            assert memory.write_lesson(wrong)
            memory.embed_fn = None
            for text in fillers:
                assert memory.write_lesson(text)
            rows = memory.get_lessons()
            assert len(rows) == 12, "dedup merged fixture rows"
            assert sum(row["embedding"] is not None for row in rows) == 2

            memory.embed_fn = embed
            recall_query = memory.startup_lesson_query(query)
            assert recall_query.vector == [1.0, 0.0, 0.0]

            block = self.render(memory, query, recall_query)

            assert block.index(right) < block.index(wrong)
        finally:
            memory.close()

    @pytest.mark.parametrize("median_words", [8, 50])
    def test_one_rare_word_in_a_median_length_row_is_worth_the_same_in_any_store(
        self, tmp_path, median_words: int
    ) -> None:
        """A median-length row sharing one rare word takes 0.1 of the keyword half, short rules or long.

        Twenty rows: ``probe`` and fifteen others are ``median_words`` long and
        four are three times that, which leaves the median at ``median_words``
        and lifts the mean to 1.4 times it. ``probe`` shares one word,
        ``quartz``, with the request and no other row carries it, so its keyword
        half is 0.1 and it scores 0.4 x 0.1 = 0.040 with no vector. Two
        embedded rows that share no word bracket it: ``above`` at cosine 0.07
        scores 0.042 and ``below`` at cosine 0.06 scores 0.036.

        Fails if the share moves: measuring length absolutely gives ``probe``
        a half of 1 / sqrt(median_words), 0.354 or 0.141; the mean in place of
        the median gives 0.118, and a scale without the rarity divisor 0.264.
        Each lifts ``probe`` above ``above``, as does a constant of 0.105 or
        more; one under 0.09 sinks it below ``below``.
        """
        above = _distinct_rows(1, median_words, "h")[0]
        below = _distinct_rows(1, median_words, "l")[0]
        probe = "Stack quartz " + " ".join(f"p0w{word}" for word in range(median_words - 2))
        fillers = _distinct_rows(13, median_words, "f") + _distinct_rows(4, 3 * median_words, "x")
        query = "calibrate quartz"

        def embed(text: str) -> list[float]:
            if text.startswith("Stack h0w"):
                return [0.07, (1 - 0.07**2) ** 0.5, 0.0]
            if text.startswith("Stack l0w"):
                return [0.06, 0.0, (1 - 0.06**2) ** 0.5]
            return [1.0, 0.0, 0.0]

        memory = VectorMemoryStore(db_path=tmp_path / "median.db")
        memory.init()
        try:
            # Written first, while an embedder is bound: a later write with an
            # embedder bound would lazily backfill the rows that must stay
            # unembedded.
            memory.embed_fn = embed
            assert memory.write_lesson(above)
            assert memory.write_lesson(below)
            memory.embed_fn = None
            for text in (probe, *fillers):
                assert memory.write_lesson(text)
            rows = memory.get_lessons()
            assert len(rows) == 20, "dedup merged fixture rows"
            assert sum(row["embedding"] is not None for row in rows) == 2

            memory.embed_fn = embed
            recall_query = memory.startup_lesson_query(query)
            assert recall_query.vector == [1.0, 0.0, 0.0]

            block = self.render(memory, query, recall_query)

            assert block.index(above) < block.index(probe) < block.index(below)
        finally:
            memory.close()
