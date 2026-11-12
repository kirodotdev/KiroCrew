"""The whole-log read for a tree edge decodes only lines that can be one.

``find_last_tree_edge`` reads a unit's whole log when its tail window holds no
adoption or release. Nearly every line of a busy log is something else, so a
JSON parse per line is the cost of the read. These tests pin what is parsed, and
that the answer is what a parse of every line would give.
"""

from __future__ import annotations

import pytest

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog
from kiro_crew.crew_log import store as crew_store
from kiro_crew.crew_log import store_retention
from kiro_crew.crew_log.schema import Entry, serialize

GATEWAY = "gateway"
FILLER = "x" * 900


@pytest.fixture
def unit(tmp_path, monkeypatch):
    """A session log, opened, that the tests append to and then bury."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    handle = CrewLog.create(lg.KIND_SESSION, "s-a", owner="raymond", agent="kirocrew", slot="A")
    handle.append(
        "session/opened",
        {
            "agent": "kirocrew",
            "slot": "A",
            "model": "opus",
            "cwd": "/w",
            "owner": "raymond",
            "resumed": False,
        },
        src=GATEWAY,
    )
    return handle


def _bury(handle: CrewLog, text: str = FILLER) -> int:
    """Append turns until the tail window cannot reach what came before. The count."""
    path = crew_store.newest_segment(crew_store.unit_dir_for(lg.KIND_SESSION, "s-a"))
    target = path.stat().st_size + crew_store._TAIL_WINDOW * 2
    written = 0
    while path.stat().st_size < target:
        handle.append(
            "message/received",
            {"turn": 1, "role": "user", "source": "peer", "text": text},
            src=GATEWAY,
        )
        written += 1
    return written


@pytest.fixture
def parsed(monkeypatch):
    """How many lines the whole-log read handed to the JSON parser.

    Patched through the store facade, which reaches every owner of the name, so
    :func:`_whole_read` zeroes the count once the log is written.
    """
    seen = {"lines": 0}
    real = crew_store._parses_to_object

    def counting(blob):
        seen["lines"] += 1
        return real(blob)

    monkeypatch.setattr(crew_store, "_parses_to_object", counting)
    return seen


def _whole_read(handle: CrewLog, parsed: dict | None = None):
    directory = crew_store.unit_dir_for(lg.KIND_SESSION, "s-a")
    del handle
    segment = crew_store.newest_segment(directory)
    if parsed is not None:
        parsed["lines"] = 0
    return crew_store._scan_whole_for_types(segment, crew_store._TREE_EDGE_TYPES)


def test_a_buried_decision_is_found_and_the_turns_after_it_are_not_parsed(unit, parsed):
    unit.append("session/adopted", {"parent": {"slot": "D"}}, src=GATEWAY)
    turns = _bury(unit)

    entry = _whole_read(unit, parsed)

    assert entry is not None and entry.data["parent"]["slot"] == "D"
    assert parsed["lines"] == 1, f"parsed {parsed['lines']} lines of a log with {turns} turns"


def test_a_log_with_no_decision_parses_nothing(unit, parsed):
    _bury(unit)

    assert _whole_read(unit, parsed) is None
    assert parsed["lines"] == 0


def test_the_newest_of_two_decisions_wins(unit):
    unit.append("session/adopted", {"parent": {"slot": "D"}}, src=GATEWAY)
    _bury(unit)
    unit.append("session/released", {}, src=GATEWAY)
    _bury(unit)

    entry = _whole_read(unit)

    assert entry is not None and entry.type == "session/released"


def test_a_turn_whose_text_is_a_decision_type_is_not_one(unit, parsed):
    """CONTROL for the skip: the type is still checked after the parse.

    The text has to BE the type. Quoted inside a longer text it is escaped on disk,
    so it does not hold the bytes the skip looks for.
    """
    turns = _bury(unit, text="session/adopted")

    assert _whole_read(unit, parsed) is None
    assert parsed["lines"] == turns, "a line holding the needle must still be parsed"


def test_a_decision_type_quoted_inside_a_text_is_not_even_parsed(unit, parsed):
    _bury(unit, text='the user typed "session/adopted" ' + FILLER)

    assert _whole_read(unit, parsed) is None
    assert parsed["lines"] == 0


@pytest.mark.parametrize("entry_type", sorted(store_retention._TREE_EDGE_TYPES))
def test_a_written_decision_holds_the_bytes_the_skip_looks_for(entry_type):
    line = serialize(Entry(type=entry_type, data={}, src=GATEWAY, seq=2, time=7).to_dict())

    assert store_retention._type_needle(entry_type) in line.encode("utf-8")
