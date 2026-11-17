"""``kirocrew learn remove`` cleans its query the way ``learn_remove`` does.

A stored rule is the sanitized form (trimmed, NFC, invisible characters
dropped), and the MCP tool cleans its query through ``LEARN_REMOVE_SCHEMA``
before matching. The CLI must do the same, or a query with a trailing space or
a decomposed accent misses the lesson the tool would remove.
"""

from __future__ import annotations

import argparse
import json
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew import cli_commands
from kiro_crew.learn import LessonStore
from kiro_crew.vector_memory import VectorMemoryStore

STORED_RULE = "order a caf\u00e9 au lait"  # NFC, as every surface stores it


def _run_remove(tmp_path, query: str, stored: str | list[str] = STORED_RULE) -> LessonStore:
    rules = [stored] if isinstance(stored, str) else stored
    (tmp_path / "lessons.jsonl").write_text(
        "".join(
            json.dumps({"ts": f"t{i}", "rule": rule, "category": "preference"}) + "\n"
            for i, rule in enumerate(rules)
        ),
        encoding="utf-8",
    )
    jsonl = LessonStore(base_dir=tmp_path)
    store = VectorMemoryStore(db_path=tmp_path / "m.db", embedding_dim=4)
    store.init()
    args = argparse.Namespace(learn_action="remove", query=query, repo_scope=None)
    try:
        with (
            patch.object(cli_commands, "VectorMemoryStore", return_value=store),
            patch.object(cli_commands, "LessonStore", return_value=jsonl),
            patch.object(cli_commands.KiroCrewConfig, "load", return_value=MagicMock()),
        ):
            cli_commands._learn(args)
    finally:
        store.close()
    return jsonl


def test_a_decomposed_query_with_a_trailing_space_removes_the_stored_lesson(
    tmp_path, capsys
) -> None:
    query = "cafe\u0301 au lait "  # NFD accent plus a trailing space
    jsonl = _run_remove(tmp_path, query)
    assert jsonl.load_all() == []
    assert "Removed lessons matching" in capsys.readouterr().out


def test_a_query_that_is_only_invisible_characters_is_refused(tmp_path, capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        _run_remove(tmp_path, "\u200b\u200b")
    assert exc.value.code == 2
    assert [le.rule for le in LessonStore(base_dir=tmp_path).load_all()] == [STORED_RULE]


def test_a_rule_stored_unsanitized_is_still_removed_by_the_raw_query(tmp_path) -> None:
    """Consolidation can store a rule it never sanitized. The cleaned query
    misses it, so the raw one is tried and the lesson is still removed."""
    nfd_rule = "order a cafe\u0301 au lait"
    jsonl = _run_remove(tmp_path, "cafe\u0301 au lait", stored=nfd_rule)
    assert jsonl.load_all() == []


def test_a_cleaned_match_does_not_leave_a_raw_only_match_in_force(tmp_path) -> None:
    """With an NFC and an unsanitized NFD rule stored together, one decomposed
    query removes both: the cleaned form matches one, the raw form the other."""
    nfd_rule = "never deploy cafe\u0301 production migrations"
    jsonl = _run_remove(tmp_path, "cafe\u0301", stored=[STORED_RULE, nfd_rule])
    assert jsonl.load_all() == []
