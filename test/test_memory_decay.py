"""The memory page's strength for an experience is the factor V1 recall multiplies in."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from test_memory_edit import store  # noqa: F401  (the v1 / v2 store fixture)

from kiro_crew import memory_decay, memory_edit
from kiro_crew import vector_memory as vm
from kiro_crew.vector_memory import VectorMemoryStore

# One unit vector per row: near-duplicates are refused, and a row's own vector
# matches it at a cosine of exactly 1.
_ROWS = {
    "fresh note about the deploy": ([1.0, 0.0, 0.0, 0.0], 2, 0.5, None),
    "old note about the deploy": ([0.0, 1.0, 0.0, 0.0], 40, 0.5, None),
    "kept note about the deploy": ([0.0, 0.0, 1.0, 0.0], 400, 0.2, ["keep"]),
    "almost faded note on the deploy": ([0.0, 0.0, 0.0, 1.0], 24, 0.5, None),
}


def _backdate(memory: VectorMemoryStore, text: str, days: int) -> None:
    # The extra hour keeps (now - created).days at exactly `days` while the test runs.
    created = (datetime.now(tz=timezone.utc) - timedelta(days=days, hours=1)).isoformat()
    memory.db.execute("UPDATE episodic_memories SET created_at = ? WHERE text = ?", (created, text))
    memory.db.commit()


def _v1(tmp_path: Path, opened) -> VectorMemoryStore:
    store = opened(  # noqa: F811
        VectorMemoryStore(db_path=tmp_path / "mem.db", embedding_dim=4, decay_rates={"keep": 0.0})
    )
    store.init()
    for text, (vector, days, importance, tags) in _ROWS.items():
        assert store.write_episodic(text, embedding=vector, importance=importance, tags=tags)
        _backdate(store, text, days)
    return store


@pytest.mark.parametrize("numpy", [True, False], ids=["vectorised", "stdlib"])
def test_the_shown_strength_is_recall_score_at_an_exact_match(tmp_path, opened, monkeypatch, numpy):
    store = _v1(tmp_path, opened)  # noqa: F811
    if not numpy:
        monkeypatch.setattr(vm, "_HAS_NUMPY", False)
    monkeypatch.setattr(vm, "_HAS_FAISS", False)
    recalled = {}
    for text, (vector, *_rest) in _ROWS.items():
        hits = store.search_episodic(query_embedding=vector, query_text="q", mmr=False, limit=10)
        recalled[text] = next(hit["score"] for hit in hits if hit["text"] == text)
    listed = memory_edit.list_records(store, {"kind": "episode"})["entries"]
    shown = {entry["text"]: entry["decay"]["strength"] for entry in listed}
    assert set(shown) == set(recalled)
    for text, score in recalled.items():
        assert shown[text] == pytest.approx(score, abs=1e-4), text


def test_bands_rate_source_and_days_to_the_fade_line(tmp_path, opened):
    store = _v1(tmp_path, opened)  # noqa: F811
    body = memory_edit.list_records(store, {"kind": "episode"}, facets=True)
    decay = {entry["text"]: entry["decay"] for entry in body["entries"]}
    # Importance 0.5 starts at 0.85; at 0.03 a day it is under 0.385 from day 27.
    assert decay["fresh note about the deploy"]["days_to_line"] == 25
    assert decay["fresh note about the deploy"]["state"] == memory_decay.STRONG
    assert decay["almost faded note on the deploy"]["days_to_line"] == 3
    assert decay["almost faded note on the deploy"]["state"] == memory_decay.FADING
    assert decay["old note about the deploy"]["days_to_line"] == 0
    assert decay["old note about the deploy"]["state"] == memory_decay.FADED
    # A rate-0 tag never fades, whatever its age.
    kept = decay["kept note about the deploy"]
    assert (kept["rate"], kept["rate_tag"], kept["strength"]) == (0.0, "keep", 0.76)
    assert kept["days_to_line"] is None and kept["state"] == memory_decay.STRONG
    assert decay["old note about the deploy"]["rate_tag"] is None
    assert body["facets"]["strength"] == {"strong": 2, "fading": 1, "faded": 1}
    # The rail's day per rate is the same crossing the rows use: 27 at the default rate.
    assert body["decay"] == {
        "fade_line": 0.385,
        "importance": 0.5,
        "start": 0.85,
        "rates": [
            {"tag": None, "rate": 0.03, "days_to_line": 27},
            {"tag": "keep", "rate": 0.0, "days_to_line": None},
        ],
    }


def test_the_fade_line_is_recalls_score_for_a_new_row_at_the_gate():
    from kiro_crew.vector_memory_runtime import episodic_search

    gate = episodic_search._EPISODIC_RELEVANCE_THRESHOLD
    assert memory_decay.fade_line() == round(episodic_search.recall_score(gate, 0.0, 0.03, 0), 4)


def test_strength_filters_and_sorts_the_list(tmp_path, opened):
    store = _v1(tmp_path, opened)  # noqa: F811
    faded = memory_edit.list_records(store, {"filters": {"strength": ["faded", "fading"]}})
    assert sorted(entry["text"] for entry in faded["entries"]) == [
        "almost faded note on the deploy",
        "old note about the deploy",
    ]
    weakest = memory_edit.list_records(store, {}, sort="strength", descending=False)
    assert [entry["text"] for entry in weakest["entries"]] == [
        "old note about the deploy",
        "almost faded note on the deploy",
        "kept note about the deploy",
        "fresh note about the deploy",
    ]


def test_a_store_that_does_not_decay_shows_no_strength(store):  # noqa: F811
    assert store.write_episodic("Reran shard 8.", conversation_id="c1")
    body = memory_edit.list_records(store, {"kind": "episode"}, facets=True)
    if store.algorithm_version == "v2":
        assert "decay" not in body["entries"][0]
        assert "decay" not in body
        assert body["facets"]["strength"] == {}
    else:
        assert body["entries"][0]["decay"]["state"] == memory_decay.STRONG


def test_a_row_under_the_line_is_faded_and_a_rate_of_zero_never_crosses():
    line = memory_decay.fade_line()
    assert memory_decay.days_to_line(line - 0.01, 0.0, 0) == 0
    assert memory_decay.days_to_line(1.0, 0.0, 9) is None
    # A subnormal rate passes the config clamp; its crossing is never, not an overflow.
    assert memory_decay.days_to_line(0.85, 1e-310, 0) is None
    assert memory_decay.state(None) == memory_decay.STRONG


def _later(monkeypatch, days: int) -> None:
    """Move the record list's clock forward by *days*, with no write to the store."""
    real = memory_edit.datetime

    class Later(real):  # type: ignore[misc, valid-type]
        @classmethod
        def now(cls, tz=None):
            return real.now(tz) + timedelta(days=days)

    monkeypatch.setattr(memory_edit, "datetime", Later)


def test_a_cached_listing_ages_its_rows_without_a_write(tmp_path, opened, monkeypatch):
    store = _v1(tmp_path, opened)  # noqa: F811
    query = {"kind": "episode", "filters": {"strength": ["faded"]}}
    before = memory_edit.list_records(store, query, sort="strength", facets=True)
    assert before["facets"]["strength"] == {"strong": 2, "fading": 1, "faded": 1}
    # Four days on, the row three days from the line is under it, though the store is unchanged.
    _later(monkeypatch, 4)
    after = memory_edit.list_records(store, query, sort="strength", facets=True)
    assert sorted(entry["text"] for entry in after["entries"]) == [
        "almost faded note on the deploy",
        "old note about the deploy",
    ]
    assert after["facets"]["strength"] == {"strong": 2, "faded": 2}
    assert {entry["text"]: entry["decay"]["state"] for entry in after["entries"]} == {
        "almost faded note on the deploy": memory_decay.FADED,
        "old note about the deploy": memory_decay.FADED,
    }


def test_a_reloaded_rate_reaches_a_cached_listing(tmp_path, opened):
    store = _v1(tmp_path, opened)  # noqa: F811
    query = {"kind": "episode", "filters": {"strength": ["faded"]}}
    assert len(memory_edit.list_records(store, query, sort="strength")["entries"]) == 1
    # As VectorMemoryStore.reconfigure applies a hot-reloaded memory.decay_rates.
    store._decay_default = 0.0
    assert memory_edit.list_records(store, query, sort="strength")["entries"] == []
