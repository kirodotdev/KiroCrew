"""What a consolidation pass reads about the semantic rows its bounded table cannot show.

``score_consolidation_rows`` ranks a store's active rows against the user text of the
span being consolidated with the chat path's hybrid score, from ONE embed of that text
and the vectors the rows were stored with. The rotation cursor is the last key the
table's rotating slice showed: it lives in the store file's ``memory_meta``, so it
survives a restart, and it moves only by compare-and-set, so two passes that read the
same cursor can show one slice twice but never skip the next.

Every store here is real, a V1 file or a member database on ``tmp_path``. The embedder
maps a few fixed words to one-hot vectors, so every cosine is exactly 0 or 1 and every
score below is an exact number.
"""

from __future__ import annotations

import math
import re
import time
from pathlib import Path

import pytest

from kiro_crew import memory_stores
from kiro_crew.embeddings_settings import (
    _MAX_EMBED_CHARS,
    PRIORITY_BULK,
    PRIORITY_NORMAL,
    EmbeddingWork,
    _work_for_priority,
    embedding_work,
)
from kiro_crew.vector_memory import (
    VectorMemoryStore,
    create_member_database,
    open_member_database,
)

_DIM = 1024
#: Word -> (axis, sign). ``kayak`` and ``canoe`` share an axis, so a canoe query reaches a
#: kayak row through its vector alone, with no word in common; ``south`` points opposite
#: ``north``, for a cosine of exactly -1.
_AXES = {
    "kayak": (0, 1.0),
    "canoe": (0, 1.0),
    "piano": (1, 1.0),
    "garden": (2, 1.0),
    "north": (3, 1.0),
    "south": (3, -1.0),
}
_SPARE_AXIS = _DIM - 1


def _one_hot(text: str) -> list[float]:
    """The axis of the first known word in *text*, or the spare axis when none is known."""
    vector = [0.0] * _DIM
    for word in re.findall(r"[a-z]+", text.lower()):
        if word in _AXES:
            axis, sign = _AXES[word]
            vector[axis] = sign
            return vector
    vector[_SPARE_AXIS] = 1.0
    return vector


def _v1_store(path: Path, opened, *, embed: bool = True) -> VectorMemoryStore:
    store = opened(VectorMemoryStore(db_path=path, embedding_dim=_DIM))
    store.init()
    if embed:
        store.embed_fn = _one_hot
    return store


_FILLER = "project.filler_"


def _words(seed: int, count: int) -> str:
    """*count* letter-only words of five letters, starting ``aa``-``af``: no query here
    uses one, and no embedder axis knows one."""
    words = []
    for index in range(count):
        number, word = seed * 100 + index, ""
        for _ in range(5):
            number, letter = divmod(number, 26)
            word = chr(ord("a") + letter) + word
        words.append(word)
    return " ".join(words)


def _fill(store: VectorMemoryStore, count: int = 20) -> None:
    """Rows that share no word with any query, as most of a real table does.

    A word held by more than a tenth of the rows, and by more than one, does not
    count, so a table of two or three rows would make every shared word common; these
    keep the rows a test names a small share of the table.
    """
    for index in range(count):
        assert (
            store.set_semantic(f"{_FILLER}{index:02d}", _words(index, 4), 0.9, "user_explicit")
            is None
        )


def _seed(store: VectorMemoryStore) -> None:
    """Three rows, the kayak row first, so neither write order nor key order puts it first."""
    for key, value in (
        ("project.zz_boat", "paddle a kayak at dawn"),
        ("project.yard", "garden needs water"),
        ("project.music", "weekly piano lessons"),
    ):
        assert store.set_semantic(key, value, 0.9, "user_explicit") is None
    _fill(store)


def _scores(
    store: VectorMemoryStore, query: str, *, deadline: float | None = None
) -> dict[str, float]:
    """The score of every row a test wrote, the filler rows left out."""
    rows = store.get_all_semantic()
    scores = store.score_consolidation_rows(
        rows, [r["value_json"] for r in rows], query, deadline=deadline
    )
    assert scores is not None
    return {
        row["key"]: score for row, score in zip(rows, scores) if not row["key"].startswith(_FILLER)
    }


# ── the score ──────────────────────────────────────────────────────────────────────


def test_a_row_the_span_talks_about_ranks_first_on_its_vector_alone(tmp_path, opened) -> None:
    store = _v1_store(tmp_path / "memory.db", opened)
    _seed(store)

    # No word of the query appears in any row; only the stored kayak vector matches.
    scores = _scores(store, "canoe saturday")

    assert scores == {
        "project.music": 0.0,
        "project.yard": 0.0,
        "project.zz_boat": pytest.approx(0.6),
    }


def test_without_an_embedder_rows_score_on_keywords_alone(tmp_path, opened) -> None:
    store = _v1_store(tmp_path / "memory.db", opened, embed=False)
    _seed(store)

    # "yard" is a key word (weight 3) and "garden" a value word (weight 1): 4 of 10.
    scores = _scores(store, "yard garden")

    assert scores == {
        "project.music": 0.0,
        "project.yard": pytest.approx(0.4),
        "project.zz_boat": 0.0,
    }


def test_a_row_with_no_stored_vector_scores_on_the_keyword_term_of_the_same_scale(
    tmp_path, opened
) -> None:
    store = _v1_store(tmp_path / "memory.db", opened)
    assert (
        store.set_semantic(
            "project.yard", "garden needs water", 0.9, "user_explicit", defer_embedding=True
        )
        is None
    )
    assert store.set_semantic("project.flowers", "garden roses", 0.9, "user_explicit") is None
    assert store.get_semantic("project.yard")["embedding"] is None
    _fill(store)

    scores = _scores(store, "yard garden")

    # The query has a vector, so the vectorless row keeps the 0.4 keyword weight
    # rather than outranking its embedded sibling on an unweighted keyword score.
    assert scores == {
        "project.flowers": pytest.approx(0.6 + 0.4 * 0.1),
        "project.yard": pytest.approx(0.4 * 0.4),
    }


def test_an_opposite_vector_scores_no_lower_than_a_missing_one(tmp_path, opened) -> None:
    store = _v1_store(tmp_path / "memory.db", opened)
    assert store.set_semantic("project.compass", "head south", 0.9, "user_explicit") is None
    assert store.set_semantic("project.map", "head west", 0.9, "user_explicit") is None
    _fill(store)

    # A cosine of -1 is clamped to 0, as the chat path clamps it: the row is
    # dissimilar, not penalised below a row whose vector says nothing.
    scores = _scores(store, "north head")

    assert scores == {
        "project.compass": pytest.approx(0.4 * 0.1),
        "project.map": pytest.approx(0.4 * 0.1),
    }


def test_words_most_rows_share_do_not_count(tmp_path, opened) -> None:
    store = _v1_store(tmp_path / "memory.db", opened, embed=False)
    for index in range(30):
        value = f"the plan is on track {_words(index, 3)}"
        assert store.set_semantic(f"project.p_{index:02d}", value, 0.9, "user_explicit") is None
    assert (
        store.set_semantic("project.boat", "the kayak is on the roof", 0.9, "user_explicit") is None
    )

    # A span says many things. Counted, its common words would score every plan
    # row 0.5 and the one row it is about 0.4; only the word few rows hold counts.
    scores = _scores(store, "the plan is on track and the kayak is on the car")

    assert scores.pop("project.boat") == pytest.approx(0.1)
    assert set(scores.values()) == {0.0}


def test_on_a_small_table_a_word_one_row_holds_still_counts(tmp_path, opened) -> None:
    store = _v1_store(tmp_path / "memory.db", opened, embed=False)
    for key, value in (
        ("project.r0", "paddle a kayak"),
        ("project.r1", "garden roses"),
        ("project.r2", "garden water"),
        ("project.r3", "piano lessons"),
        ("project.r4", "north trail"),
    ):
        assert store.set_semantic(key, value, 0.9, "user_explicit") is None

    # Five rows leave out a word more than max(1, int(5 * 0.1)) = 1 of them hold:
    # "kayak" is held by one row, a fifth of them, and counts; "garden" by two does not.
    assert _scores(store, "kayak garden") == {
        "project.r0": pytest.approx(0.1),
        "project.r1": 0.0,
        "project.r2": 0.0,
        "project.r3": 0.0,
        "project.r4": 0.0,
    }


def test_a_recorded_identity_counts_as_part_of_the_key(tmp_path, opened) -> None:
    store = _v1_store(tmp_path / "memory.db", opened, embed=False)
    assert (
        store.set_semantic(
            "user.contact.primary",
            "alice at example dot com",
            0.9,
            "user_explicit",
            metadata={"subject": "alice", "predicate": "work_email"},
        )
        is None
    )
    _fill(store)

    # The predicate's two words count as key words, as the chat path counts them.
    assert _scores(store, "her work email changed") == {"user.contact.primary": pytest.approx(0.6)}


def test_a_key_word_after_an_underscore_counts(tmp_path, opened) -> None:
    store = _v1_store(tmp_path / "memory.db", opened, embed=False)
    _seed(store)

    assert _scores(store, "boat")["project.zz_boat"] == pytest.approx(0.3)


def test_capitals_on_either_side_still_match(tmp_path, opened) -> None:
    store = _v1_store(tmp_path / "memory.db", opened, embed=False)
    assert store.set_semantic("project.trip", "Paddle a Kayak", 0.9, "user_explicit") is None
    _fill(store)

    assert _scores(store, "Kayak tomorrow") == {"project.trip": pytest.approx(0.1)}


def test_the_query_is_embedded_once_at_bulk_priority_from_its_tail(
    tmp_path, opened, monkeypatch
) -> None:
    store = _v1_store(tmp_path / "memory.db", opened)
    _seed(store)
    calls: list[tuple[str, int]] = []
    real = store._try_embed

    def spy(text: str, priority: int = 0):
        calls.append((text, priority))
        return real(text, priority)

    monkeypatch.setattr(store, "_try_embed", spy)
    # The head names the garden, the tail the canoe: only the tail reaches the embedder.
    query = "garden " + "x " * _MAX_EMBED_CHARS + "canoe"

    scores = _scores(store, query)

    assert calls == [(query[-_MAX_EMBED_CHARS:], PRIORITY_BULK)]
    assert "garden" not in calls[0][0]
    assert scores["project.zz_boat"] == pytest.approx(0.6)
    # The keyword overlap still reads the whole query: the head's "garden" counts.
    assert scores["project.yard"] == pytest.approx(0.4 * 0.1)


def test_a_space_change_before_the_scan_ends_drops_the_vector(
    tmp_path, opened, monkeypatch
) -> None:
    store = _v1_store(tmp_path / "memory.db", opened)
    _seed(store)
    real = store._try_embed

    def embed_then_swap(text: str, priority: int = 0):
        vector = real(text, priority)
        store._space_generation += 1
        return vector

    monkeypatch.setattr(store, "_try_embed", embed_then_swap)

    scores = _scores(store, "canoe yard")

    # Keyword-only, unweighted: the vector-only kayak match is gone and the key
    # match scores its full 3 of 10.
    assert scores == {
        "project.music": 0.0,
        "project.yard": pytest.approx(0.3),
        "project.zz_boat": 0.0,
    }


def test_an_embedder_that_raises_leaves_the_keyword_scores(tmp_path, opened) -> None:
    store = _v1_store(tmp_path / "memory.db", opened)
    _seed(store)

    def broken(_text: str) -> list[float]:
        raise RuntimeError("embedder down")

    store.embed_fn = broken

    assert _scores(store, "canoe yard") == {
        "project.music": 0.0,
        "project.yard": pytest.approx(0.3),
        "project.zz_boat": 0.0,
    }


def _queued(works: list[EmbeddingWork]):
    """``_one_hot`` behind a queue that, like the shared embedder's, resolves the
    calling thread's work and answers ``None`` once that work has expired."""

    def embed(text: str, *, priority: int = PRIORITY_NORMAL) -> list[float] | None:
        work = _work_for_priority(priority)
        works.append(work)
        return None if work.expired() else _one_hot(text)

    embed.accepts_priority = True  # type: ignore[attr-defined]
    return embed


#: The canoe query's scores with no vector: only the yard key's word counts.
_KEYWORD_ONLY = {
    "project.music": 0.0,
    "project.yard": pytest.approx(0.3),
    "project.zz_boat": 0.0,
}


@pytest.mark.parametrize("ahead", [0.0, 3600.0, None], ids=["passed", "ahead", "none"])
def test_a_deadline_bounds_the_query_embed_and_its_expiry_leaves_the_keywords(
    tmp_path, opened, ahead
) -> None:
    store = _v1_store(tmp_path / "memory.db", opened)
    _seed(store)
    works: list[EmbeddingWork] = []
    store.embed_fn = _queued(works)
    deadline = None if ahead is None else time.monotonic() + ahead

    scores = _scores(store, "canoe yard", deadline=deadline)

    # The embed ran under the deadline, at bulk priority, and the thread's own work
    # is back as it was. With none, bulk work waits as long as the queue takes.
    [work] = works
    assert (work.deadline, work.priority) == (
        math.inf if deadline is None else deadline,
        PRIORITY_BULK,
    )
    assert embedding_work.get() is None
    if ahead == 0.0:
        assert scores == _KEYWORD_ONLY
    else:
        assert scores == {
            "project.music": 0.0,
            "project.yard": pytest.approx(0.4 * 0.3),
            "project.zz_boat": pytest.approx(0.6),
        }


def test_a_tighter_deadline_the_caller_set_still_holds(tmp_path, opened) -> None:
    store = _v1_store(tmp_path / "memory.db", opened)
    _seed(store)
    works: list[EmbeddingWork] = []
    store.embed_fn = _queued(works)
    caller = EmbeddingWork(time.monotonic())
    previous = embedding_work.get()
    embedding_work.set(caller)
    try:
        scores = _scores(store, "canoe yard", deadline=time.monotonic() + 3600.0)
    finally:
        embedding_work.set(previous)

    [work] = works
    assert work.deadline == caller.deadline
    # A caller's cancellation still reaches the embed.
    assert work.cancelled is caller.cancelled
    assert scores == _KEYWORD_ONLY


def test_a_failure_inside_the_scorer_returns_no_signal_instead_of_raising(tmp_path, opened) -> None:
    store = _v1_store(tmp_path / "memory.db", opened)
    _seed(store)
    rows = store.get_all_semantic()
    texts = [r["value_json"] for r in rows]

    def raising(_query):
        raise RuntimeError("scorer down")

    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(store, "_stored_similarity_scorer", raising)
        assert store.score_consolidation_rows(rows, texts, "canoe") is None
    # Texts that are not one to one with the rows are no signal either.
    assert store.score_consolidation_rows(rows, texts[:-1], "canoe") is None
    assert store.score_consolidation_rows(rows, texts, "canoe") is not None


def test_an_empty_query_embeds_nothing_and_scores_zero(tmp_path, opened, monkeypatch) -> None:
    store = _v1_store(tmp_path / "memory.db", opened)
    _seed(store)
    monkeypatch.setattr(store, "_try_embed", lambda *_a, **_k: pytest.fail("embedded"))

    assert set(_scores(store, "   ").values()) == {0.0}


# ── an embedder that arrives after the store opened ──────────────────────────────────


def _reopened_before_its_model(path: Path, opened, factory) -> VectorMemoryStore:
    """The seeded file at *path* opened again with no embedder bound, as a gateway that
    started before its model was on disk opens it, and *factory* to bind one from.

    The rows keep the vectors they were written with. The re-bind cooldown is off, as
    the re-bind tests have it, so the first embed calls the factory however recently
    the monotonic clock started.
    """
    seeded = _v1_store(path, opened)
    _seed(seeded)
    seeded.close()
    store = _v1_store(path, opened, embed=False)
    store.embed_fn_factory = factory
    store._embed_fn_rebind_cooldown_secs = 0.0
    return store


def test_a_store_opened_before_its_model_binds_it_and_scores_on_vectors(tmp_path, opened) -> None:
    built: list[None] = []

    def factory():
        built.append(None)
        return _one_hot

    store = _reopened_before_its_model(tmp_path / "memory.db", opened, factory)

    # The first pass binds the embedder, as every other embed does, and the kayak row
    # ranks on its vector alone: no word of the query is in any row.
    scores = _scores(store, "canoe saturday")

    assert built == [None]
    assert store.embed_fn is _one_hot
    assert scores == {
        "project.music": 0.0,
        "project.yard": 0.0,
        "project.zz_boat": pytest.approx(0.6),
    }


def _no_embedder():
    return None


def _model_missing():
    raise RuntimeError("model missing")


def _probe_fails():
    return lambda _text: None


@pytest.mark.parametrize(
    "factory", [_no_embedder, _model_missing, _probe_fails], ids=["none", "raises", "probe-fails"]
)
def test_a_factory_with_no_embedder_to_give_leaves_the_keyword_scores(
    tmp_path, opened, factory
) -> None:
    built: list[None] = []

    def counted():
        built.append(None)
        return factory()

    store = _reopened_before_its_model(tmp_path / "memory.db", opened, counted)

    scores = _scores(store, "canoe yard")

    # The re-bind was tried, found nothing to bind, and raised nothing.
    assert built == [None]
    assert store.embed_fn is None
    assert scores == _KEYWORD_ONLY


@pytest.mark.parametrize("ahead", [3600.0, 0.0], ids=["ahead", "passed"])
def test_the_deadline_bounds_the_embed_after_a_rebind(tmp_path, opened, ahead) -> None:
    """A deadline already passed cuts the re-bind's probe short as well: nothing is
    bound, for a later embed to bind, and the rows score on keywords."""
    works: list[EmbeddingWork] = []
    embedder = _queued(works)
    store = _reopened_before_its_model(tmp_path / "memory.db", opened, lambda: embedder)
    deadline = time.monotonic() + ahead

    scores = _scores(store, "canoe yard", deadline=deadline)

    # The probe, then the query's embed, ran under the pass's deadline at bulk priority.
    assert {(work.deadline, work.priority) for work in works} == {(deadline, PRIORITY_BULK)}
    assert embedding_work.get() is None
    if ahead:
        assert len(works) == 2
        assert store.embed_fn is embedder
        assert scores == {
            "project.music": 0.0,
            "project.yard": pytest.approx(0.4 * 0.3),
            "project.zz_boat": pytest.approx(0.6),
        }
    else:
        assert len(works) == 1
        assert store.embed_fn is None
        assert scores == _KEYWORD_ONLY


# ── the rotation cursor ──────────────────────────────────────────────────────────────


def test_the_cursor_survives_a_restart(tmp_path, opened) -> None:
    path = tmp_path / "memory.db"
    store = _v1_store(path, opened)
    assert store.consolidation_rotation_cursor() is None

    assert store.advance_consolidation_rotation(None, "project.b") is True
    store.close()
    reopened = _v1_store(path, opened)

    assert reopened.consolidation_rotation_cursor() == "project.b"


def test_the_cursor_moves_only_from_the_value_it_was_read_at(tmp_path, opened) -> None:
    store = _v1_store(tmp_path / "memory.db", opened)
    assert store.advance_consolidation_rotation(None, "project.b") is True

    # A pass that read no cursor, or a stale one, lost the race: nothing moves.
    assert store.advance_consolidation_rotation(None, "project.z") is False
    assert store.advance_consolidation_rotation("project.a", "project.z") is False
    assert store.consolidation_rotation_cursor() == "project.b"

    assert store.advance_consolidation_rotation("project.b", "project.c") is True
    assert store.consolidation_rotation_cursor() == "project.c"


#: The ``memory_meta`` key the cursor is stored under, as a file on disk spells it.
_CURSOR_KEY = "consolidation.semantic_rotation_after"


def _write_blob_cursor(store: VectorMemoryStore, value: bytes) -> None:
    """Store *value* under the cursor key by hand. The column is ``TEXT``, but SQLite
    keeps a BLOB written into it as a BLOB."""
    store.db.execute(
        "INSERT INTO memory_meta (key, value, updated_at) VALUES (?, ?, ?)",
        (_CURSOR_KEY, value, "2026-01-01T00:00:00+00:00"),
    )
    store.db.commit()
    stored = store.db.execute("SELECT typeof(value) FROM memory_meta WHERE key = ?", (_CURSOR_KEY,))
    assert stored.fetchone()[0] == "blob"


def test_a_non_text_cursor_reads_as_none_and_the_next_advance_replaces_it(tmp_path, opened) -> None:
    store = _v1_store(tmp_path / "memory.db", opened)
    _write_blob_cursor(store, b"project.x")

    assert store.consolidation_rotation_cursor() is None
    # A text expectation never matches it, not even one spelling the same bytes.
    assert store.advance_consolidation_rotation("project.x", "project.z") is False
    assert store.advance_consolidation_rotation(None, "project.b") is True
    assert store.consolidation_rotation_cursor() == "project.b"

    # The next pass moves on, and the cursor is compare-and-set again.
    assert store.advance_consolidation_rotation("project.b", "project.c") is True
    assert store.advance_consolidation_rotation("project.b", "project.z") is False
    assert store.advance_consolidation_rotation(None, "project.z") is False
    assert store.consolidation_rotation_cursor() == "project.c"


def test_two_passes_that_read_a_non_text_cursor_replace_it_once(tmp_path, opened) -> None:
    path = tmp_path / "memory.db"
    first = _v1_store(path, opened)
    second = _v1_store(path, opened)
    _write_blob_cursor(first, b"project.x")
    assert first.consolidation_rotation_cursor() is None
    assert second.consolidation_rotation_cursor() is None

    assert first.advance_consolidation_rotation(None, "project.b") is True
    assert second.advance_consolidation_rotation(None, "project.e") is False
    assert second.consolidation_rotation_cursor() == "project.b"


def test_an_unreadable_cursor_reads_as_none(tmp_path, opened) -> None:
    store = _v1_store(tmp_path / "memory.db", opened)
    assert store.advance_consolidation_rotation(None, "project.b") is True
    store.close()

    assert store.consolidation_rotation_cursor() is None


def test_an_advance_on_a_closed_store_reports_false_instead_of_raising(
    tmp_path, opened, caplog
) -> None:
    store = _v1_store(tmp_path / "memory.db", opened)
    store.close()

    with caplog.at_level("WARNING", logger="kiro_crew.vector_memory"):
        assert store.advance_consolidation_rotation(None, "project.b") is False
    assert "rotation cursor not advanced" in caplog.text
    # The log names the failure, never the key it was moving to.
    assert "project.b" not in caplog.text


def test_a_failed_advance_leaves_no_open_transaction(tmp_path, opened) -> None:
    store = _v1_store(tmp_path / "memory.db", opened)
    _seed(store)
    # The statement itself fails, inside the transaction the DML opened.
    store.db.execute(
        "CREATE TRIGGER refuse_meta BEFORE INSERT ON memory_meta "
        "BEGIN SELECT RAISE(ABORT, 'refused'); END"
    )
    store.db.commit()

    assert store.advance_consolidation_rotation(None, "project.b") is False

    assert not store.db.in_transaction
    store.db.execute("DROP TRIGGER refuse_meta")
    store.db.commit()
    assert store.advance_consolidation_rotation(None, "project.b") is True


# ── a member database ────────────────────────────────────────────────────────────────


def test_a_member_database_scores_its_rows_and_keeps_its_cursor(
    tmp_path, opened, monkeypatch
) -> None:
    monkeypatch.setattr(memory_stores, "memory_stores_root", lambda: tmp_path / "stores")
    path = tmp_path / "member" / "memory.db"
    path.parent.mkdir()
    create_member_database(path, member_id="alice", store_id="member-alice")

    def member() -> VectorMemoryStore:
        store = opened(
            open_member_database(
                path, member_id="alice", store_id="member-alice", embedding_dim=_DIM
            )
        )
        store.embed_fn = _one_hot
        return store

    store = member()
    assert store.algorithm_version == "v2"
    _seed(store)
    rows = store.with_record_metadata(store.get_all_semantic())

    scores = store.score_consolidation_rows(rows, [r["value_json"] for r in rows], "canoe")

    named = {k: s for k, s in zip((r["key"] for r in rows), scores or []) if _FILLER not in k}
    assert named == {
        "project.music": 0.0,
        "project.yard": 0.0,
        "project.zz_boat": pytest.approx(0.6),
    }
    assert store.advance_consolidation_rotation(None, "project.music") is True
    assert store.advance_consolidation_rotation(None, "project.yard") is False
    store.close()
    assert member().consolidation_rotation_cursor() == "project.music"
