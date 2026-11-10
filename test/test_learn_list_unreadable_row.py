"""``kirocrew learn list`` survives a lesson row whose stored JSON is damaged.

The row is skipped and named on stderr, every other lesson is still listed, and
the exit status is non-zero so a script sees that a row could not be read. When
no vector row decodes, the JSONL file is listed, as GET /api/lessons does.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew import cli_commands
from kiro_crew.learn import LessonStore
from kiro_crew.vector_memory import VectorMemoryStore


def _damage(tmp_path, rules: list[str], damaged_word: str) -> str:
    """Write ``rules`` to the vector store, then break the one holding the word."""
    store = VectorMemoryStore(db_path=tmp_path / "m.db", embedding_dim=4)
    store.init()
    try:
        for rule in rules:
            assert store.write_lesson(rule, "preference")
        key = next(row["key"] for row in store.get_lessons() if damaged_word in row["value_json"])
    finally:
        store.close()
    db = sqlite3.connect(tmp_path / "m.db")
    try:
        with db:
            db.execute(
                "UPDATE semantic_memory SET value_json = ? WHERE key = ?", ("{not json", key)
            )
    finally:
        db.close()
    return key


def _list(tmp_path) -> int:
    store = VectorMemoryStore(db_path=tmp_path / "m.db", embedding_dim=4)
    store.init()
    try:
        with (
            patch.object(cli_commands, "VectorMemoryStore", return_value=store),
            patch.object(cli_commands, "LessonStore", return_value=LessonStore(base_dir=tmp_path)),
            patch.object(cli_commands.KiroCrewConfig, "load", return_value=MagicMock()),
            pytest.raises(SystemExit) as exc,
        ):
            cli_commands._learn(argparse.Namespace(learn_action="list"))
    finally:
        store.close()
    return exc.value.code


def test_one_unreadable_row_is_skipped_and_the_rest_are_listed(tmp_path, capsys) -> None:
    key = _damage(tmp_path, ["prefer tabs for indentation", "run the gate before pushing"], "tabs")
    assert _list(tmp_path) == 1
    out = capsys.readouterr()
    assert "run the gate before pushing" in out.out
    assert key in out.err
    assert "1 lesson row(s) could not be read" in out.err


def test_an_unreadable_only_vector_tier_lists_the_jsonl_lessons(tmp_path, capsys) -> None:
    key = _damage(tmp_path, ["prefer tabs for indentation"], "tabs")
    (tmp_path / "lessons.jsonl").write_text(
        json.dumps({"ts": "t0", "rule": "keep commits small", "category": "preference"}) + "\n",
        encoding="utf-8",
    )
    assert _list(tmp_path) == 1
    out = capsys.readouterr()
    assert "keep commits small" in out.out
    assert key in out.err
