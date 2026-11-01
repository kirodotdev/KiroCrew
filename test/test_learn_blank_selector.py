"""A blank ``learn`` argument is refused before it can touch a lesson.

Every rule contains the empty string, so a blank ``kirocrew learn remove``
selector (an unset ``"$VAR"``) would match every lesson, and a blank
``learn add`` would save a lesson with no rule. The CLI refuses both with exit
2, and both stores' delete refuses a blank substring itself, so no other caller
can reach the match-everything path.
"""

from __future__ import annotations

import argparse
import json
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew import cli_commands
from kiro_crew.learn import LessonStore
from kiro_crew.vector_memory import VectorMemoryStore

BLANKS = ["", "   ", "\t\n"]


def _store(tmp_path) -> VectorMemoryStore:
    store = VectorMemoryStore(db_path=tmp_path / "m.db", embedding_dim=4)
    store.init()
    return store


def _seed_jsonl(tmp_path) -> LessonStore:
    lines = [
        {"ts": "t0", "rule": "use tabs", "category": "preference"},
        {"ts": "t1", "rule": "run the gate before pushing", "category": "tool"},
    ]
    (tmp_path / "lessons.jsonl").write_text(
        "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8"
    )
    return LessonStore(base_dir=tmp_path)


@pytest.mark.parametrize("blank", BLANKS)
def test_cli_remove_refuses_a_blank_selector_and_keeps_every_lesson(
    tmp_path, capsys, blank
) -> None:
    store = _store(tmp_path)
    jsonl = _seed_jsonl(tmp_path)
    try:
        assert store.write_lesson("prefer tabs", "preference")
        assert store.write_lesson("run the gate before pushing", "tool")
        args = argparse.Namespace(learn_action="remove", query=blank, repo_scope=None)
        with (
            patch.object(cli_commands, "VectorMemoryStore", return_value=store),
            patch.object(cli_commands, "LessonStore", return_value=jsonl),
            patch.object(cli_commands.KiroCrewConfig, "load", return_value=MagicMock()),
            pytest.raises(SystemExit) as exc,
        ):
            cli_commands._learn(args)
        assert exc.value.code == 2
        assert "rule substring required" in capsys.readouterr().err
        assert len(store.get_lessons()) == 2
    finally:
        store.close()
    assert len(jsonl.load_all()) == 2


@pytest.mark.parametrize("blank", BLANKS)
def test_cli_add_refuses_a_blank_rule_before_any_store(capsys, blank) -> None:
    vs_cls = MagicMock()
    args = argparse.Namespace(learn_action="add", rule=blank, category="knowledge", negative=None)
    with (
        patch.object(cli_commands, "VectorMemoryStore", vs_cls),
        patch.object(cli_commands, "LessonStore", return_value=MagicMock()),
        patch.object(cli_commands.KiroCrewConfig, "load", return_value=MagicMock()),
        pytest.raises(SystemExit) as exc,
    ):
        cli_commands._learn(args)
    assert exc.value.code == 2
    assert "rule is required" in capsys.readouterr().err
    vs_cls.assert_not_called()


@pytest.mark.parametrize("blank", BLANKS)
def test_vector_delete_refuses_a_blank_substring(tmp_path, blank) -> None:
    store = _store(tmp_path)
    try:
        assert store.write_lesson("prefer tabs", "preference")
        assert store.write_lesson("run the gate before pushing", "tool")
        with pytest.raises(ValueError, match="blank"):
            store.delete_lesson(blank)
        with pytest.raises(ValueError, match="blank"):
            store.delete_lesson(blank, "")
        assert len(store.get_lessons()) == 2
        # exact and keep_one cannot take every lesson, so they stay open.
        assert store.delete_lesson(blank, exact=True) is False
        assert store.delete_lesson(blank, keep_one=True) is False
        assert len(store.get_lessons()) == 2
    finally:
        store.close()


@pytest.mark.parametrize("blank", BLANKS)
def test_jsonl_remove_refuses_a_blank_substring(tmp_path, blank) -> None:
    store = _seed_jsonl(tmp_path)
    with pytest.raises(ValueError, match="blank"):
        store.remove(blank)
    with pytest.raises(ValueError, match="blank"):
        store.remove(blank, "")
    assert len(store.load_all()) == 2
    assert store.remove(blank, exact=True) is False
    assert store.remove(blank, keep_one=True) is False
    assert len(store.load_all()) == 2
