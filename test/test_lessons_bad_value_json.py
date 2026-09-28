"""A lesson row whose stored JSON does not decode is skipped, not fatal."""

from __future__ import annotations

import logging
from pathlib import Path

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
