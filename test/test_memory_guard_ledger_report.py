"""The memory guard's report on a bound worker's item (rfc-host-memory-guard.md §8 test 10).

One ``report`` event with ``status: question`` and no reason; the item's prior
``artifacts``, ``pr`` and ``discussion`` unchanged; no write for an unbound session;
the conductor's report wake moves; the commit is undone when the record does not land.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
import work_ledger_helpers
from work_ledger_helpers import CONDUCTOR, WORKER, _new_item

from kiro_crew import conductor_wake, ledger_wake
from kiro_crew import work_ledger as wl

_isolated_home = work_ledger_helpers._isolated_home

SUMMARY = "memory guard: stopped pytest (3.0 GB) at 1.4 GB free; the worker keeps running"
DISCUSSION = {"topic": "t", "state": "open"}


def _bound_item_with_pointers() -> str:
    item_id = _new_item()
    wl.apply_conductor_action(CONDUCTOR, "bind", item_id=item_id, worker_session_key=WORKER)
    wl.apply_worker_report(
        CONDUCTOR,
        item_id,
        status="blocked",
        summary="waiting on CI",
        artifacts={"branch": "feat/x"},
        pr=123,
    )
    return item_id


def test_the_store_writes_one_question_and_keeps_the_workers_pointers() -> None:
    item_id = _bound_item_with_pointers()
    before = wl.read_work_item(CONDUCTOR, item_id)
    out = wl.apply_guard_report(CONDUCTOR, item_id, summary=SUMMARY)
    after = wl.read_work_item(CONDUCTOR, item_id)
    assert out["event"].kind == "report"
    assert (after.status, after.reason, after.summary) == ("question", None, SUMMARY)
    assert after.artifacts == before.artifacts == {"branch": "feat/x"}
    assert after.pr == before.pr == 123
    assert after.discussion == before.discussion
    reports = [e for e in wl.read_events(CONDUCTOR, item_id) if e.kind == "report"]
    assert [e.status for e in reports] == ["blocked", "question"]
    # A question is never a repeated wait, so the conductor's report wake fires.
    assert not ledger_wake.repeats_status("report", "question", "question")
    assert conductor_wake._report_stamp(
        after.to_dict() | {"events": []}
    ) != conductor_wake._report_stamp(before.to_dict() | {"events": []})


@pytest.mark.parametrize(
    ("status", "reason"), [("done", None), ("blocked", "needs_human"), ("question", "approval")]
)
def test_a_done_or_reasoned_report_is_left_alone(status: str, reason: Any) -> None:
    item_id = _bound_item_with_pointers()
    wl.apply_worker_report(
        CONDUCTOR, item_id, status=status, summary="theirs", reason=reason, pr=123
    )
    events_before = len(wl.read_events(CONDUCTOR, item_id))
    assert wl.apply_guard_report(CONDUCTOR, item_id, summary=SUMMARY) is None
    after = wl.read_work_item(CONDUCTOR, item_id)
    assert (after.status, after.reason, after.summary) == (status, reason, "theirs")
    assert len(wl.read_events(CONDUCTOR, item_id)) == events_before


def test_a_closed_item_is_refused() -> None:
    item_id = _bound_item_with_pointers()
    wl.apply_conductor_action(CONDUCTOR, "close", item_id=item_id, state="abandoned")
    with pytest.raises(wl.WorkLedgerError):
        wl.apply_guard_report(CONDUCTOR, item_id, summary=SUMMARY)


@pytest.fixture
def _recorded(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    from kiro_crew.crew_log import emit as crew_log_emit
    from kiro_crew.dashboard.handlers import work_ledger as guard

    entries: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(crew_log_emit, "enabled", lambda: True)
    monkeypatch.setattr(guard, "_acting_unit", lambda *_a, **_k: ("unit-w", None))
    monkeypatch.setattr(guard, "_conductor_unit", lambda *_a, **_k: "unit-c")

    def fake_record(unit: str, data: dict[str, Any], **_k: Any) -> bool:
        entries.append((unit, data))
        return True

    monkeypatch.setattr(crew_log_emit, "on_work_recorded", fake_record)
    return entries


def test_the_route_records_the_boards_copy(_recorded: list[tuple[str, dict[str, Any]]]) -> None:
    from kiro_crew.dashboard.memory_guard_report import record_guard_report

    item_id = _bound_item_with_pointers()
    got = asyncio.run(record_guard_report(SimpleNamespace(), WORKER, SUMMARY))
    assert got == item_id
    assert len(_recorded) == 1
    unit, entry = _recorded[0]
    assert unit == "unit-c" and entry["board_copy"] is True
    assert (entry["actor"], entry["action"], entry["status"]) == ("worker", "report", "question")
    assert "reason" not in entry or entry["reason"] is None
    assert entry["artifacts"] == {"branch": "feat/x"} and entry["pr"] == 123


def test_a_prewarmed_conductor_unit_falls_back_to_the_boards_newest_log(
    monkeypatch: pytest.MonkeyPatch, _recorded: list[tuple[str, dict[str, Any]]]
) -> None:
    from kiro_crew import session_ledger
    from kiro_crew.crew_log import emit as crew_log_emit
    from kiro_crew.dashboard.memory_guard_report import record_guard_report

    item_id = _bound_item_with_pointers()
    landed: list[str] = []

    def record(unit: str, data: dict[str, Any], **_k: Any) -> bool:
        landed.append(unit)
        return unit == "unit-old"  # the prewarmed live unit has no log yet

    monkeypatch.setattr(crew_log_emit, "on_work_recorded", record)
    monkeypatch.setattr(session_ledger, "newest_work_unit", lambda _slot: "unit-old")
    assert asyncio.run(record_guard_report(SimpleNamespace(), WORKER, SUMMARY)) == item_id
    assert landed == ["unit-c", "unit-old"]
    assert wl.read_work_item(CONDUCTOR, item_id).status == "question"


def test_an_unbound_session_gets_no_write(_recorded: list[tuple[str, dict[str, Any]]]) -> None:
    from kiro_crew.dashboard.memory_guard_report import record_guard_report

    assert asyncio.run(record_guard_report(SimpleNamespace(), "chat-not-bound", SUMMARY)) == ""
    assert _recorded == []


def test_an_unrecorded_write_is_undone(monkeypatch: pytest.MonkeyPatch, _recorded: Any) -> None:
    from kiro_crew.crew_log import emit as crew_log_emit
    from kiro_crew.dashboard.memory_guard_report import record_guard_report

    item_id = _bound_item_with_pointers()
    monkeypatch.setattr(crew_log_emit, "on_work_recorded", lambda *_a, **_k: False)
    assert asyncio.run(record_guard_report(SimpleNamespace(), WORKER, SUMMARY)) == ""
    item = wl.read_work_item(CONDUCTOR, item_id)
    assert (item.status, item.summary) == ("blocked", "waiting on CI")


def test_an_entry_that_cannot_be_built_is_undone(
    monkeypatch: pytest.MonkeyPatch, _recorded: Any
) -> None:
    from kiro_crew.dashboard.handlers import work_ledger as wlh
    from kiro_crew.dashboard.memory_guard_report import record_guard_report

    item_id = _bound_item_with_pointers()

    def boom(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise ValueError("baseline nested too deep")

    monkeypatch.setattr(wlh, "_entry_with_baseline", boom)
    assert asyncio.run(record_guard_report(SimpleNamespace(), WORKER, SUMMARY)) == ""
    item = wl.read_work_item(CONDUCTOR, item_id)
    assert (item.status, item.summary) == ("blocked", "waiting on CI")
    assert _recorded == []
