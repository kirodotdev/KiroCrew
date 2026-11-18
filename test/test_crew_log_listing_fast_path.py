"""The listing fast path (row 6), and what the read budget reports around it.

Real store, real readers: what is counted is the read gateway code makes, on a unit
written through ``CrewLog``.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog
from kiro_crew.crew_log import eager as crew_log_eager
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.crew_log import read as crew_read
from kiro_crew.crew_log import read_budget
from kiro_crew.dashboard.handlers import work_ledger_board

SLOT = "chat-listing-fast-path"
BIG = 4 * read_budget.K


@pytest.fixture(autouse=True)
def _isolated(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    _floor_monkeypatch.setenv("KIROCREW_CREW_LOG", "1")
    _floor_monkeypatch.delenv(read_budget.MODE_ENV, raising=False)
    _floor_monkeypatch.setattr(crew_log_eager, "note_commit", lambda *a, **k: None)
    crew_log_emit.reset_caches()
    crew_log.forget_slot_folds()
    crew_log.forget_session_folds()
    read_budget.reset_for_tests()
    yield
    read_budget.reset_for_tests()
    crew_log.forget_slot_folds()
    crew_log.forget_session_folds()


def _unit(unit_id: str, records: int = BIG) -> None:
    handle = CrewLog.create(lg.KIND_SESSION, unit_id, owner="o", agent="a", slot=SLOT)
    for at in range(0, records, 1000):
        batch = range(at, min(records, at + 1000))
        handle.append_many(
            [{"type": "ledger/recorded", "data": {"slot": SLOT, "goal": f"g{i}"}} for i in batch],
            src="gateway",
        )
    handle.release_ownership()


def _walk(unit_id: str, seq: int = 1, **kwargs) -> int:
    handle = CrewLog.open(lg.KIND_SESSION, unit_id)
    try:
        return sum(1 for _ in handle.iter_from(seq, **kwargs))
    finally:
        handle.release_ownership()


def test_a_strict_prefix_walk_and_a_cold_fold_pass_are_reported():
    _unit("acp-prefix")
    read_budget.mark_gateway_process()
    assert _walk("acp-prefix", BIG) == 1  # walks BIG - 1 records below its answer
    assert crew_log.read_slot_projection(SLOT, "ledger").seq == BIG  # cold fold pass
    counts = read_budget.snapshot()
    assert counts["over_budget"] == 2


def test_a_listed_row_resumes_from_the_status_fold_and_matches_the_walk(monkeypatch):
    for unit_id in ("acp-list-a", "acp-list-b"):
        _unit(unit_id)
    walked = crew_read.list_session_units(limit=10)
    with monkeypatch.context() as patch:
        patch.setattr(crew_read, "segment_first_seqs", lambda *_a: [])  # force the walk
        expected = crew_read.list_session_units(limit=10)
    assert json.dumps(walked, sort_keys=True) == json.dumps(expected, sort_keys=True)
    read_budget.mark_gateway_process()
    decoded: "list[int]" = []
    real = read_budget._settle

    def spy(tally):
        decoded.append(tally.records)
        real(tally)

    monkeypatch.setattr(read_budget, "_settle", spy)
    again = crew_read.list_session_units(limit=10)
    assert json.dumps(again, sort_keys=True) == json.dumps(expected, sort_keys=True)
    assert decoded and max(decoded) <= read_budget.K + 1, decoded
    assert read_budget.snapshot()["over_budget"] == 0


def test_conductor_asks_on_a_cold_slot_answers_as_main_does():
    handle = CrewLog.create(lg.KIND_SESSION, "acp-asks", owner="o", agent="a", slot=SLOT)
    handle.append_many(
        [{"type": "ledger/recorded", "data": {"slot": SLOT, "goal": f"g{i}"}} for i in range(BIG)],
        src="gateway",
    )
    asks = {"it_1": {"options": ["Approve", "Revise"]}}
    handle.append(
        "dashboard/agentic_value",
        {"field": "asks", "type": "object", "value": {"v": asks}},
        src="gateway",
    )
    handle.release_ownership()
    read_budget.mark_gateway_process()
    # No warm cell and no savepoint: the same answer a cold fold gives, on this request.
    assert work_ledger_board._conductor_asks(SLOT, {"it_1"}) == {"it_1": ["Approve", "Revise"]}
    assert work_ledger_board._conductor_asks(SLOT, {"it_1"}) == {"it_1": ["Approve", "Revise"]}


def test_a_listed_row_counts_an_unfamiliar_entry_type_as_the_walk_does():
    _unit("acp-list-new")
    handle = CrewLog.open(lg.KIND_SESSION, "acp-list-new")
    handle.append("message/from-a-newer-writer", {"n": 1}, src="gateway", ignorable=True)
    handle.append("ledger/recorded", {"slot": SLOT, "goal": "after"}, src="gateway")
    handle.release_ownership()
    crew_read.list_session_units(limit=10)  # first listing: writes the status savepoint
    listed = crew_read.list_session_units(limit=10)
    row = next(r for r in listed["units"] if r["unit"] == "acp-list-new")
    assert row["entries"] == BIG + 2 and row["last_seq"] == BIG + 2


def test_a_listed_row_falls_back_to_the_walk_when_the_status_fold_refuses(monkeypatch):
    from kiro_crew.crew_log.errors import CrewLogError

    _unit("acp-list-refused")
    expected = crew_read.list_session_units(limit=10)

    def refuse(*_a, **_k):
        raise CrewLogError("refused", code="bad_data")

    monkeypatch.setattr(crew_read.projections, "fold_session", refuse)
    assert crew_read.list_session_units(limit=10) == expected
