"""``kirocrew learn add --applies`` states a lesson's tier as ``learn_add`` does.

The ``learn_add`` MCP tool takes ``applies`` (``always`` / ``on_topic``) and
``POST /api/lessons`` stores it after the origin gate. The CLI offers the same
two values, validates them through the same ``LEARN_ADD_SCHEMA``, stores the
stated tier, and leaves the row unstated when the option is omitted.
"""

from __future__ import annotations

import argparse
import json
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew import cli_commands
from kiro_crew.cli_parser_agent import register_learn_parser
from kiro_crew.lesson_validation import LESSON_APPLIES_VALUES
from kiro_crew.mcp_tools import learn as mcp_learn
from kiro_crew.vector_memory import VectorMemoryStore

_RULE = "keep release notes to one page"


def _parse(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    register_learn_parser(parser.add_subparsers(dest="command"))
    return parser.parse_args(["learn", "add", *argv])


def _store(tmp_path) -> VectorMemoryStore:
    store = VectorMemoryStore(db_path=tmp_path / "m.db", embedding_dim=4)
    store.init()
    return store


def _stored(tmp_path) -> list[dict]:
    """Every stored lesson's value, read through a fresh handle."""
    reader = _store(tmp_path)
    try:
        return [json.loads(e["value_json"]) for e in reader.get_lessons()]
    finally:
        reader.close()


def _run(tmp_path, args: argparse.Namespace, *, gate: str, in_agent_session: bool) -> None:
    cfg = MagicMock()
    cfg.memory.lesson_origin_gate = gate
    with (
        patch.object(cli_commands, "VectorMemoryStore", return_value=_store(tmp_path)),
        patch.object(cli_commands, "LessonStore", return_value=MagicMock()),
        patch.object(cli_commands.KiroCrewConfig, "load", return_value=cfg),
        patch("kiro_crew.cloud.aws._in_agent_session", return_value=in_agent_session),
    ):
        cli_commands._learn(args)


def test_the_option_offers_exactly_the_tiers_the_mcp_tool_offers() -> None:
    [tool] = [t for t in mcp_learn.schemas() if t["name"] == "learn_add"]
    mcp_enum = tool["inputSchema"]["properties"]["applies"]["enum"]
    assert list(LESSON_APPLIES_VALUES) == mcp_enum
    for tier in mcp_enum:
        assert _parse(_RULE, "--applies", tier).applies == tier


def test_an_omitted_option_parses_as_unstated() -> None:
    assert _parse(_RULE).applies is None


def test_an_unknown_tier_is_refused_by_the_parser(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        _parse(_RULE, "--applies", "sometimes")
    assert exc.value.code == 2
    assert "--applies" in capsys.readouterr().err


@pytest.mark.parametrize("tier", LESSON_APPLIES_VALUES)
def test_a_stated_tier_is_stored_from_a_persons_terminal(tmp_path, capsys, tier) -> None:
    _run(tmp_path, _parse(_RULE, "--applies", tier), gate="enforce", in_agent_session=False)

    assert "Saved:" in capsys.readouterr().out
    [row] = _stored(tmp_path)
    assert row["rule"] == _RULE and row["applies"] == tier


def test_an_omitted_tier_stores_no_applies(tmp_path, capsys) -> None:
    _run(tmp_path, _parse(_RULE), gate="enforce", in_agent_session=False)

    assert "Saved:" in capsys.readouterr().out
    [row] = _stored(tmp_path)
    assert "applies" not in row


def test_a_stated_tier_reaches_the_origin_gate_in_an_agent_session(tmp_path, capsys) -> None:
    # Shadow mode only records, so the stored tier is the one the gate was handed.
    _run(tmp_path, _parse(_RULE, "--applies", "always"), gate="shadow", in_agent_session=True)

    assert "Saved:" in capsys.readouterr().out
    [row] = _stored(tmp_path)
    assert row["applies"] == "always"


def test_enforce_still_demotes_a_call_no_person_made(tmp_path, capsys) -> None:
    # Stating ``always`` from a shell inside an agent session does not get
    # around the gate: it is stored on_topic, as POST /api/lessons stores it.
    _run(tmp_path, _parse(_RULE, "--applies", "always"), gate="enforce", in_agent_session=True)

    capsys.readouterr()
    [row] = _stored(tmp_path)
    assert row["applies"] == "on_topic"
