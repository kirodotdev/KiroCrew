"""A ``lesson.*`` key the model puts in the ``semantic`` array is a lesson.

The consolidation prompt asks for ``pref.*`` / ``project.*`` / ``user.*`` keys in
the ``semantic`` array and for lessons in ``lessons``, but the split is not
enforced. A ``semantic`` item keyed ``lesson.<something>`` would otherwise be
written straight through ``set_semantic`` (V1) / ``_write_semantic`` (V2),
skipping the exact-rule dedup, the volatile-fact screen and the
extraction-enabled gate that the lessons path applies -- which lets the same
lesson be stored several times under different model-chosen keys.

``_reroute_lesson_semantic_keys`` moves such a create into the ``lessons`` array
once, on the shared ``result``, before it branches to either write path. These
tests pin the helper directly and then prove the downstream effect on both the
V1 and V2 write paths: two passes that re-state one rule under two different
``lesson.*`` semantic keys leave ONE deduped lesson row and zero ``lesson.*``
rows that bypassed ``write_lesson``.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest
from member_memory_helpers import declare_v2_store

from kiro_crew.history import HistoryConsolidator
from kiro_crew.history_consolidation_memory import _reroute_lesson_semantic_keys
from kiro_crew.vector_memory import VectorMemoryStore, open_member_database


def _reroute(result: dict) -> dict:
    return _reroute_lesson_semantic_keys(result, logging.getLogger("test"))


# --- the helper, in isolation ------------------------------------------------


def test_lesson_semantic_create_is_moved_to_lessons() -> None:
    """A ``lesson.*`` create leaves ``semantic`` and becomes a lesson by its value."""
    result = {
        "semantic": [
            {"key": "pref.editor", "value": "vim", "confidence": 0.9},
            {
                "key": "lesson.set_idiom_refused",
                "value": "Never use the set -- $A idiom; the policy scanner refuses it.",
                "confidence": 0.9,
                "category": "tool",
                "negative": "do not run set -- $A",
            },
        ]
    }
    out = _reroute(result)
    # The real fact stays; the lesson key is gone from semantic.
    assert [item["key"] for item in out["semantic"]] == ["pref.editor"]
    assert len(out["lessons"]) == 1
    lesson = out["lessons"][0]
    assert lesson == {
        "rule": "Never use the set -- $A idiom; the policy scanner refuses it.",
        "category": "tool",
        "negative": "do not run set -- $A",
    }


def test_lesson_semantic_create_appends_to_existing_lessons() -> None:
    """A rerouted lesson is appended after the model's own ``lessons`` entries."""
    result = {
        "semantic": [{"key": "lesson.x", "value": "rule two"}],
        "lessons": [{"rule": "rule one"}],
    }
    out = _reroute(result)
    assert [item["rule"] for item in out["lessons"]] == ["rule one", "rule two"]
    assert out["semantic"] == []


def test_lesson_semantic_passthrough_fields_carried() -> None:
    """``applies`` and ``repo_scope`` ride along to the lessons path."""
    result = {
        "semantic": [
            {
                "key": "lesson.y",
                "value": "a scoped standing rule",
                "applies": "always",
                "repo_scope": "src/kiro_crew",
            }
        ]
    }
    out = _reroute(result)
    assert out["lessons"][0] == {
        "rule": "a scoped standing rule",
        "applies": "always",
        "repo_scope": "src/kiro_crew",
    }


@pytest.mark.parametrize("value", [True, 123, {"nested": "object"}, ["a"], None, "", "   "])
def test_lesson_semantic_non_text_value_is_dropped(value) -> None:
    """A ``lesson.*`` item whose value is not non-empty text is not a lesson: dropped.

    It is NOT left in ``semantic`` -- leaving it there would reintroduce exactly
    the un-deduped ``lesson.*`` semantic write this reroute removes.
    """
    out = _reroute_lesson_semantic_keys(
        {"semantic": [{"key": "lesson.z", "value": value}]},
        logging.getLogger("test"),
    )
    assert out["semantic"] == []
    assert out.get("lessons", []) == []


def test_lesson_semantic_surrogate_value_is_forwarded_and_refused_without_crash() -> None:
    """A lone-surrogate rule is forwarded by the reroute and refused by write_lesson.

    The reroute forwards any non-empty string (it does not know how the value is
    stored); the lessons derive site (``write_lesson``) owns the UTF-8 check,
    because the same crash reaches it through the model's own ``lessons`` array.
    Routing a ``\\ud800`` rule must therefore NOT raise and must store no row.
    """
    out = _reroute_lesson_semantic_keys(
        {"semantic": [{"key": "lesson.surrogate", "value": "Preserve \ud800 please"}]},
        logging.getLogger("test"),
    )
    # The reroute forwards it (it is a non-empty string).
    assert out["semantic"] == []
    assert out["lessons"] == [{"rule": "Preserve \ud800 please"}]


def test_write_lesson_refuses_a_surrogate_rule_or_scope(tmp_path) -> None:
    """write_lesson refuses a non-UTF-8 rule or scope instead of crashing.

    This is the single derive site the native ``lessons`` array and the
    rerouted ``lesson.*`` semantic items both reach, so the guard belongs here.
    """
    store = VectorMemoryStore(db_path=tmp_path / "m.db", embedding_dim=4)
    store.init()
    try:
        assert not store.write_lesson("Preserve \ud800 please", "knowledge")
        assert not store.write_lesson("a clean rule", "knowledge", repo_scope="src/\ud800")
        assert _lesson_rows(store) == [], "a non-UTF-8 lesson must store no row"
    finally:
        store.close()


def test_lesson_semantic_delete_stays_in_semantic() -> None:
    """A ``delete`` of a ``lesson.*`` key is left for the tier-guarded delete site."""
    result = {"semantic": [{"key": "lesson.keep", "delete": True}]}
    out = _reroute_lesson_semantic_keys(result, logging.getLogger("test"))
    assert out["semantic"] == [{"key": "lesson.keep", "delete": True}]
    assert out.get("lessons", []) == []


def test_non_lesson_keys_are_untouched() -> None:
    """A result with no ``lesson.*`` semantic key is returned unchanged (same object)."""
    result = {"semantic": [{"key": "pref.editor", "value": "vim"}]}
    out = _reroute_lesson_semantic_keys(result, logging.getLogger("test"))
    assert out is result


# --- downstream effect on the real write paths -------------------------------


def _consolidator(store: VectorMemoryStore) -> HistoryConsolidator:
    return HistoryConsolidator(
        log=MagicMock(), memory=MagicMock(), vector_store=store, migrated=True
    )


def _lesson_rows(store: VectorMemoryStore) -> list[dict]:
    return [
        row for row in store.get_all_semantic() if str(row.get("key", "")).startswith("lesson.")
    ]


def test_v1_write_path_dedups_rerouted_lesson(tmp_path) -> None:
    """Two ``lesson.*`` semantic items for one rule yield ONE deduped lesson row.

    Without the reroute, ``_write_structured_memory`` writes each ``lesson.*``
    semantic item straight through ``set_semantic`` under its own model-chosen
    key, leaving two near-identical rows that no dedup ever folds. With the
    reroute the two items go to ``_save_lessons`` -> ``write_lesson``, whose
    exact-rule dedup keeps one.
    """
    store = VectorMemoryStore(db_path=tmp_path / "m.db", embedding_dim=4)
    store.init()
    try:
        rule = "Never use the set -- $A idiom in shell commands; the scanner refuses it."
        consolidator = _consolidator(store)

        # Two passes, the SAME rule under two DIFFERENT lesson.* keys -- the
        # shape the bug takes (the model picks a fresh key each time).
        for model_key in ("lesson.set_idiom_blocked", "lesson.set_idiom_refused_again"):
            result = _reroute({"semantic": [{"key": model_key, "value": rule, "category": "tool"}]})
            # The reroute emptied `semantic` and filled `lessons`; drive the two
            # V1 writers the way the pass does with a rerouted result.
            consolidator._write_structured_memory(result, key="s1")
            consolidator._save_lessons(result.get("lessons", []), key="s1")

        rows = _lesson_rows(store)
        assert len(rows) == 1, f"expected one deduped lesson row, got {len(rows)}: {rows}"
        # The model-chosen keys never became plain semantic rows.
        assert not any(
            row["key"] in {"lesson.set_idiom_blocked", "lesson.set_idiom_refused_again"}
            for row in store.get_all_semantic()
        ), "a lesson.* key must not survive as a raw semantic row"
        # The surviving lesson row carries the rule text (stored in its value).
        assert rule in str(rows[0].get("value_json", ""))
    finally:
        store.close()


def test_v2_write_path_dedups_rerouted_lesson(tmp_path, monkeypatch) -> None:
    """The member V2 path routes a rerouted ``lesson.*`` item through its lesson branch."""
    from kiro_crew import memory_stores

    monkeypatch.setattr(memory_stores, "memory_stores_root", lambda: tmp_path / "stores")
    directory = declare_v2_store(tmp_path, "member-alice")
    store = open_member_database(
        directory / "memory.db", member_id="alice", store_id="member-alice", embedding_dim=4
    )
    assert store.algorithm_version == "v2"
    try:
        rule = "Prefer python3 -c datetime for epoch-to-date conversions, not date -d @."
        snapshot = {row["key"]: row for row in store.get_all_semantic()}

        for i, model_key in enumerate(("lesson.epoch_a", "lesson.epoch_b")):
            result = _reroute({"semantic": [{"key": model_key, "value": rule}]})
            assert result["semantic"] == []
            assert len(result["lessons"]) == 1
            store.apply_consolidation(
                source_id=f"src-{i}",
                session_key="s1",
                source_total=1,
                result=result,
                snapshot=snapshot,
                messages=[{"role": "user", "content": "hi"}],
            )

        rows = _lesson_rows(store)
        assert len(rows) == 1, f"expected one lesson row, got {len(rows)}: {rows}"
        assert not any(row["key"] in {"lesson.epoch_a", "lesson.epoch_b"} for row in rows)
    finally:
        store.close()
