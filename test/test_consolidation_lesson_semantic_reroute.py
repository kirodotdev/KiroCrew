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

import json
import logging
from unittest.mock import MagicMock

import pytest
from member_memory_helpers import declare_v2_store

import kiro_crew.vector_memory as vm
from kiro_crew.history import HistoryConsolidator
from kiro_crew.history_consolidation_memory import (
    _bounded_semantic_table,
    _reroute_lesson_semantic_keys,
    _withhold_unseen_deletes,
)
from kiro_crew.vector_memory import (
    VectorMemoryStore,
    _lesson_display_text,
    _lesson_key,
    open_member_database,
)
from kiro_crew.vector_memory_constants import _SEMANTIC_PROMPT_CAP_PER_CONSOLIDATION


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


# --- a lesson.* delete reaches its tier check only when the block showed the lesson --


#: The span's user text: it names the kayak, which only the two relevant lessons do.
_SPAN_TEXT = "Should we take the kayak out today"
#: Past every ``lesson.<md5>`` key and before ``project.*``: the rotating slice
#: then shows facts, so a lesson in the block is there by the lessons allowance.
_AFTER_THE_LESSONS = "lesson.zzzzzzzzzzzz"


def _words(seed: int, count: int) -> str:
    """*count* letter-only words no other seed shares, since ``write_lesson`` folds a
    lesson into one that shares half its significant words."""
    words = []
    for index in range(count):
        number, word = seed * 1000 + index, ""
        for _ in range(5):
            number, letter = divmod(number, 26)
            word = chr(ord("a") + letter) + word
        words.append(word)
    return " ".join(words)


def _clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """One second per stamp the store takes, so every row's recency is set, never a tie."""
    ticks = iter(range(86_400))

    def now() -> str:
        minutes, seconds = divmod(next(ticks), 60)
        return f"2026-01-01T{minutes // 60:02d}:{minutes % 60:02d}:{seconds:02d}+00:00"

    monkeypatch.setattr(vm, "_now_iso", now)


def _seed_lessons_and_facts(store: VectorMemoryStore) -> dict[str, str]:
    """Twenty lessons (two about the kayak) under a table of facts over the cap.

    The oldest lesson is written first: the lessons allowance holds the two
    relevant ones and the newest of the rest, so the oldest is left out, and the
    facts are numerous enough that the backfill never reaches a lesson.
    """
    unshown = _words(1, 90)
    assert store.write_lesson(unshown, "knowledge", applies="on_topic")
    for seed in range(2, 20):
        assert store.write_lesson(_words(seed, 90), "knowledge", applies="on_topic")
    on_topic = "Check the kayak hull for cracks " + _words(30, 80)
    standing = "Wear a vest in the kayak " + _words(31, 80)
    assert store.write_lesson(on_topic, "knowledge", applies="on_topic")
    assert store.write_lesson(standing, "knowledge", applies="always")
    for index in range(80):
        assert (
            store.set_semantic(
                f"project.f_{index:02d}", _words(100 + index, 150), 0.9, "user_explicit"
            )
            is None
        )
    return {
        "on_topic": _lesson_key(on_topic),
        "standing": _lesson_key(standing),
        "unshown": _lesson_key(unshown),
    }


def _block_keys(store: VectorMemoryStore) -> frozenset[str]:
    """The keys the pass's semantic block shows, built the way the pass builds it."""
    rows = store.get_all_semantic()
    private = store.algorithm_version == "v2"
    if private:
        rows = store.with_record_metadata(rows)
    entries = []
    for row in rows:
        value = row["value_json"]
        if row["key"].startswith("lesson."):
            value = _lesson_display_text(json.loads(value)) or value
        entry = {"key": row["key"], "value_json": value, "confidence": row["confidence"]}
        if private:
            entry |= {
                "record_revision": row.get("record_revision", 0),
                "metadata": row.get("record_metadata", {}),
            }
        entries.append(entry)
    scores = store.score_consolidation_rows(rows, [e["value_json"] for e in entries], _SPAN_TEXT)
    table = _bounded_semantic_table(
        rows,
        entries,
        _SEMANTIC_PROMPT_CAP_PER_CONSOLIDATION,
        scores=scores,
        rotation_after=_AFTER_THE_LESSONS,
    )
    assert table.omitted, "the seeded table must be over the cap"
    return table.visible_keys


def test_v1_a_lesson_the_block_shows_reaches_its_tier_check_and_an_unshown_one_does_not(
    tmp_path, monkeypatch, caplog
) -> None:
    _clock(monkeypatch)
    store = VectorMemoryStore(db_path=tmp_path / "m.db", embedding_dim=4)
    store.init()
    try:
        keys = _seed_lessons_and_facts(store)
        visible = _block_keys(store)
        assert {keys["on_topic"], keys["standing"]} <= visible
        assert keys["unshown"] not in visible
        result = _reroute({"semantic": [{"key": key, "delete": True} for key in keys.values()]})

        with caplog.at_level(logging.INFO, logger="kiro_crew.history"):
            _consolidator(store)._write_structured_memory(result, key="s1", visible_keys=visible)

        live = {row["key"] for row in store.get_all_semantic()}
        # The shown finding is retired; the shown standing rule survives its tier
        # check; the unshown finding never reaches one.
        assert keys["on_topic"] not in live
        assert {keys["standing"], keys["unshown"]} <= live
        messages = [record.getMessage() for record in caplog.records]
        tier = [m for m in messages if "refused to retire standing lesson" in m]
        assert len(tier) == 1 and keys["standing"] in tier[0]
        withheld = [m for m in messages if m.startswith("Semantic consolidation refused delete")]
        assert len(withheld) == 1 and keys["unshown"] in withheld[0]
    finally:
        store.close()


def test_member_a_lesson_the_block_shows_becomes_a_forget_proposal_and_an_unshown_one_does_not(
    tmp_path, monkeypatch
) -> None:
    from kiro_crew import memory_stores

    monkeypatch.setattr(memory_stores, "memory_stores_root", lambda: tmp_path / "stores")
    _clock(monkeypatch)
    directory = declare_v2_store(tmp_path, "member-alice")
    store = open_member_database(
        directory / "memory.db", member_id="alice", store_id="member-alice", embedding_dim=4
    )
    try:
        keys = _seed_lessons_and_facts(store)
        visible = _block_keys(store)
        assert {keys["on_topic"], keys["standing"]} <= visible
        assert keys["unshown"] not in visible
        result = {"semantic": [{"key": key, "delete": True} for key in keys.values()]}

        fenced, withheld = _withhold_unseen_deletes(result, visible, logging.getLogger("test"))
        store.apply_consolidation(
            source_id="span-1",
            session_key="s1",
            source_total=1,
            result=fenced,
            snapshot={row["key"]: row for row in store.get_all_semantic()},
            messages=[{"role": "user", "content": _SPAN_TEXT}],
        )

        assert withheld == 1
        proposals = {
            row[0]
            for row in store.db.execute(
                "SELECT record_id FROM memory_revisions "
                "WHERE operation = 'forget' AND status = 'conflict'"
            )
        }
        # Every delete is the owner's to approve on a member store, so both shown
        # lessons are proposed and nothing is proposed for the unshown one.
        assert proposals == {f"key:{keys['on_topic']}", f"key:{keys['standing']}"}
    finally:
        store.close()
