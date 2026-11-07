"""``kirocrew learn add`` validates and stores a rule like MCP and REST do.

``POST /api/lessons`` and the ``learn_add`` MCP tool both validate against
``LEARN_ADD_SCHEMA``, which sanitizes the text and caps the SANITIZED ``rule``
and ``negative`` at ``MAX_SHORT_STRING`` (500), then persists that cleaned
value. The CLI runs the same validator, refuses an over-cap rule with the same
message and exit 2 with no store touched, and stores the cleaned value -- so a
rule whose sanitized form fits is not saved over the cap in its raw form. A
rule exactly at the cap still saves.
"""

from __future__ import annotations

import argparse
import json
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew import cli_commands
from kiro_crew.constants import MAX_SHORT_STRING
from kiro_crew.validation import LEARN_ADD_SCHEMA, validate_tool_args
from kiro_crew.vector_memory import VectorMemoryStore


def _add_args(rule: str, negative: str | None = None) -> argparse.Namespace:
    return argparse.Namespace(
        learn_action="add", rule=rule, category="knowledge", negative=negative
    )


def _store(tmp_path) -> VectorMemoryStore:
    store = VectorMemoryStore(db_path=tmp_path / "m.db", embedding_dim=4)
    store.init()
    return store


def _stored_rules(tmp_path) -> list[str]:
    """Rule text of every lesson in the store at *tmp_path*, via a fresh handle."""
    reader = _store(tmp_path)
    try:
        out = []
        for e in reader.get_lessons():
            val = json.loads(e["value_json"])
            out.append(val.get("rule") if isinstance(val, dict) else str(val))
        return out
    finally:
        reader.close()


@pytest.mark.parametrize("extra", [1, 120])
def test_cli_add_refuses_an_over_limit_rule_before_any_store(capsys, extra) -> None:
    length = MAX_SHORT_STRING + extra
    vs_cls = MagicMock()
    args = _add_args("x" * length)
    with (
        patch.object(cli_commands, "VectorMemoryStore", vs_cls),
        patch.object(cli_commands, "LessonStore", return_value=MagicMock()),
        patch.object(cli_commands.KiroCrewConfig, "load", return_value=MagicMock()),
        pytest.raises(SystemExit) as exc,
    ):
        cli_commands._learn(args)
    assert exc.value.code == 2
    err = capsys.readouterr().err
    # The exact message POST /api/lessons and the learn_add MCP tool return.
    assert f"rule: exceeds max length {MAX_SHORT_STRING} (got {length}, trim {extra} chars)" in err
    vs_cls.assert_not_called()


def test_cli_add_refuses_an_over_limit_negative_clause(capsys) -> None:
    vs_cls = MagicMock()
    args = _add_args("stay concise", negative="n" * (MAX_SHORT_STRING + 5))
    with (
        patch.object(cli_commands, "VectorMemoryStore", vs_cls),
        patch.object(cli_commands, "LessonStore", return_value=MagicMock()),
        patch.object(cli_commands.KiroCrewConfig, "load", return_value=MagicMock()),
        pytest.raises(SystemExit) as exc,
    ):
        cli_commands._learn(args)
    assert exc.value.code == 2
    assert f"negative: exceeds max length {MAX_SHORT_STRING}" in capsys.readouterr().err
    vs_cls.assert_not_called()


def test_cli_add_saves_a_rule_exactly_at_the_cap(tmp_path, capsys) -> None:
    rule = "y" * MAX_SHORT_STRING
    args = _add_args(rule)
    # _learn opens its own store and closes it in a finally, so read the result
    # back through a fresh store on the same db rather than the closed handle.
    store = _store(tmp_path)
    with (
        patch.object(cli_commands, "VectorMemoryStore", return_value=store),
        patch.object(cli_commands, "LessonStore", return_value=MagicMock()),
        patch.object(cli_commands.KiroCrewConfig, "load", return_value=MagicMock()),
    ):
        cli_commands._learn(args)
    assert "Saved:" in capsys.readouterr().out
    assert _stored_rules(tmp_path) == [rule]


def test_cli_add_stores_the_sanitized_rule_not_the_raw_over_cap_form(tmp_path, capsys) -> None:
    # A rule whose sanitized form fits the cap but whose raw form does not: the
    # combining acute (NFD) collapses under the schema's normalization, so the
    # cleaned rule is shorter than the raw one. The CLI must store the cleaned
    # value POST /api/lessons persists, not the raw over-cap text.
    raw = "Prefer cafe\u0301. " * 37
    cleaned = validate_tool_args({"rule": raw, "category": "knowledge"}, LEARN_ADD_SCHEMA)["rule"]
    assert len(raw) > MAX_SHORT_STRING >= len(cleaned)
    assert cleaned != raw
    args = _add_args(raw)
    store = _store(tmp_path)
    with (
        patch.object(cli_commands, "VectorMemoryStore", return_value=store),
        patch.object(cli_commands, "LessonStore", return_value=MagicMock()),
        patch.object(cli_commands.KiroCrewConfig, "load", return_value=MagicMock()),
    ):
        cli_commands._learn(args)
    assert "Saved:" in capsys.readouterr().out
    stored = _stored_rules(tmp_path)
    assert stored == [cleaned]
    assert len(stored[0]) <= MAX_SHORT_STRING
