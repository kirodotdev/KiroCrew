"""A lesson row whose stored JSON does not decode is skipped, not fatal."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from kiro_crew.vector_memory import VectorMemoryStore

GOOD = "Run the database migration before deploying"
BAD = "Never force push to a shared branch"


def test_undecodable_row_is_skipped_and_the_good_lesson_survives(tmp_path: Path, caplog) -> None:
    store = VectorMemoryStore(db_path=tmp_path / "mem.db")
    store.init()
    try:
        store.write_lesson(GOOD)
        store.write_lesson(BAD)
        bad_key = next(r["key"] for r in store.get_lessons() if BAD in r["value_json"])
        with store._db_lock, store.db:
            store.db.execute(
                "UPDATE semantic_memory SET value_json = 'Keep lessons in sync.' WHERE key = ?",
                (bad_key,),
            )

        with caplog.at_level(logging.WARNING, logger="kiro_crew.vector_memory"):
            for background in (False, True):
                block = store.get_lessons_context(background=background)
                assert GOOD in block
                assert "Keep lessons in sync." not in block

        warnings = [r for r in caplog.records if "does not decode" in r.getMessage()]
        assert len(warnings) == 2  # one per build above, each naming the row key
        assert all(bad_key in r.getMessage() for r in warnings)
        assert all("Keep lessons in sync." not in r.getMessage() for r in warnings)
    finally:
        store.close()


def test_an_undecodable_row_does_not_block_a_delete_and_stays_removable(tmp_path: Path) -> None:
    store = VectorMemoryStore(db_path=tmp_path / "mem.db")
    store.init()
    try:
        store.write_lesson(GOOD)
        store.write_lesson(BAD)
        bad_key = next(r["key"] for r in store.get_lessons() if BAD in r["value_json"])
        with store._db_lock, store.db:
            store.db.execute(
                "UPDATE semantic_memory SET value_json = 'Keep lessons in sync.' WHERE key = ?",
                (bad_key,),
            )

        try:
            deleted = store.delete_lesson("database migration")
        except ValueError as exc:  # json.JSONDecodeError
            pytest.fail(f"one damaged row blocked the delete: {exc!r}")
        assert deleted
        assert [r["key"] for r in store.get_lessons()] == [bad_key]
        # Its scope cannot be read, so a scope-selective delete never claims it.
        assert not store.delete_lesson("lessons in sync", repo_scope="")
        # keep_one never trades a valid copy of its text for it.
        store.write_lesson(GOOD)
        copy_key = next(r["key"] for r in store.get_lessons() if r["key"] != bad_key)
        with store._db_lock, store.db:
            store.db.execute(
                "UPDATE semantic_memory SET value_json = '\"Keep lessons in sync.\"' WHERE key = ?",
                (copy_key,),
            )
        assert not store.delete_lesson("lessons in sync", keep_one=True)
        assert {r["key"] for r in store.get_lessons()} == {bad_key, copy_key}
        # Only a delete with neither reaches it, by its raw stored text.
        assert store.delete_lesson("lessons in sync")
        assert store.get_lessons() == []
    finally:
        store.close()
