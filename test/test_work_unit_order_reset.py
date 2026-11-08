"""A reset's new unit is listed LAST, so the work and panel folds stay warm across it.

``work_crew_log_units`` and ``panel_crew_log_units`` list units missing from the order
file before the recorded ones, except a unit newer than every recorded one, which goes
after them. A conductor reset opens such a unit, so its open and its first record are
appends, and the warm cell carries over both instead of refolding the slot cold.

Each test runs the real crew-log writer on a temp data home and checks the warm answer
against a from-scratch fold of the same units.
"""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew import crew_log as lg
from kiro_crew import session_ledger, session_ledger_control
from kiro_crew.crew_log import CrewLog
from kiro_crew.crew_log import eager as crew_log_eager
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.crew_log import projection_slot_memo as memo_mod
from kiro_crew.crew_log.projection_readers import _slot_units_for_fold

SLOT = "member-order-conductor"


@pytest.fixture(autouse=True)
def _home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    _floor_monkeypatch.setenv("KIROCREW_CREW_LOG", "1")
    # No background folds: the test reads every origin itself.
    _floor_monkeypatch.setattr(crew_log_eager, "note_commit", lambda *a, **k: None)
    crew_log_emit.reset_caches()
    crew_log_eager.stop_for_tests()
    crew_log.forget_slot_folds()
    yield
    # The handles this file opened hold write leases; release them so no later test
    # on this worker inherits one.
    for handle in _handles.values():
        handle.release_ownership()
    _handles.clear()
    crew_log.forget_slot_folds()
    crew_log_emit.reset_caches()


@pytest.fixture
def origins(monkeypatch) -> "list[str]":
    seen: list[str] = []
    monkeypatch.setattr(memo_mod, "_note_slot_fold_origin", seen.append)
    return seen


_handles: "dict[str, CrewLog]" = {}
_seq = [0]


def _open(unit: str) -> None:
    _handles[unit] = CrewLog.create(lg.KIND_SESSION, unit, owner="owner", agent="lead", slot=SLOT)


def _record(name: str, unit: str) -> None:
    """One entry the *name* fold reads, then the order publish its writer makes."""
    _seq[0] += 1
    if name == "work":
        data = {
            "slot": SLOT,
            "actor": "conductor",
            "by": SLOT,
            "action": "goal",
            "goal": f"goal {_seq[0]}",
            "round": _seq[0],
        }
        _handles[unit].append("work/recorded", data, src="gateway")
        session_ledger.note_work_unit_recorded(SLOT, unit)
    else:
        data = {"template": "default", "data": {"cycle": _seq[0]}, "title": f"cycle {_seq[0]}"}
        _handles[unit].append("panel/published", data, src="gateway")
        session_ledger.note_panel_unit_recorded(SLOT, unit)


def _read(name: str, origins: "list[str]") -> "tuple[tuple[str, ...], str]":
    """*name*'s units and this read's origin; asserts the warm value equals a cold fold."""
    origins.clear()
    units = _slot_units_for_fold(SLOT, name)
    warm = crew_log.fold_slot_warm(name, units, slot=SLOT).state
    cold: Any = crew_log.fold_slot_checkpoint(name, units, slot=SLOT).state
    assert warm == cold
    return units, ",".join(origins)


def _two_units(name: str, origins: "list[str]") -> None:
    for unit in ("u-1", "u-2"):
        _open(unit)
        _record(name, unit)
    _read(name, origins)


@pytest.mark.parametrize("name", ["work", "panel"])
def test_a_resets_open_and_first_record_stay_warm(name, origins):
    _two_units(name, origins)
    _open("u-3")
    units, origin = _read(name, origins)
    assert units == ("u-1", "u-2", "u-3")
    assert origin == "warm"
    _record(name, "u-3")
    units, origin = _read(name, origins)
    assert units == ("u-1", "u-2", "u-3")
    assert origin == "warm"


@pytest.mark.parametrize("name", ["work", "panel"])
def test_two_resets_in_a_row_stay_warm(name, origins):
    _two_units(name, origins)
    for unit in ("u-3", "u-4"):
        _open(unit)
        assert _read(name, origins)[1] == "warm"
        _record(name, unit)
        assert _read(name, origins)[1] == "warm"
    assert _read(name, origins)[0] == ("u-1", "u-2", "u-3", "u-4")


def test_a_skipped_reset_unit_folds_first_once_a_newer_unit_opens(origins):
    """shortcut: only the newest unit is listed last, so a reset unit that never
    records moves first when the next reset opens, and that read folds cold (same
    value). Upgrade only if back-to-back resets with no work in between show up in
    the cold-fold metric."""
    _two_units("work", origins)
    _open("u-3")
    assert _read("work", origins)[0] == ("u-1", "u-2", "u-3")
    _open("u-4")
    assert _read("work", origins)[0] == ("u-3", "u-1", "u-2", "u-4")
    _record("work", "u-4")
    assert _read("work", origins)[0] == ("u-3", "u-1", "u-2", "u-4")


def test_an_evicted_unit_a_backward_clock_sorts_last_still_folds_first(origins, monkeypatch):
    """A unit evicted from the order tail whose header a backward clock step sorts
    after the retained units is not the newest by succession, so it stays first."""
    from kiro_crew.crew_log import store

    # Ids sort in creation order, so the real succession (createdAt, then the store's
    # id tiebreak) names u-3 newest even when a coarse clock gives all three one stamp.
    for unit in ("u-1", "u-2", "u-3"):
        _open(unit)
        _record("work", unit)
    path = session_ledger_control._control_file(SLOT, session_ledger_control._WORK_UNIT_ORDER_FILE)
    session_ledger_control._rewrite_lines(path, ("u-2", "u-3"))
    # The stepped-back clock: only the work list's header listing sorts u-1 last.
    monkeypatch.setattr(store, "session_units_for_slot", lambda *_a, **_k: ("u-2", "u-3", "u-1"))
    assert session_ledger.work_crew_log_units(SLOT) == ("u-1", "u-2", "u-3")


def test_a_failed_order_write_keeps_the_new_unit_last_in_header_order(origins, monkeypatch):
    _two_units("work", origins)
    _open("u-3")

    def refused(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(session_ledger, "append_line", refused)
    _record("work", "u-3")
    assert session_ledger_control._recorded_unit_order(
        SLOT, order_file=session_ledger_control._WORK_UNIT_ORDER_FILE
    ) == ("u-1", "u-2")
    units, origin = _read("work", origins)
    assert units == ("u-1", "u-2", "u-3")
    assert origin == "warm"
    assert crew_log.fold_slot_warm("work", units, slot=SLOT).state["goal"] == f"goal {_seq[0]}"


def test_an_unrecorded_unit_older_than_a_recorded_one_still_folds_first(origins):
    _open("u-1")
    _open("u-2")
    _record("work", "u-2")
    _open("u-3")
    _record("work", "u-3")
    assert _read("work", origins)[0] == ("u-1", "u-2", "u-3")
    # Recorded order wins over header order for the recorded units.
    _record("work", "u-2")
    assert _read("work", origins)[0] == ("u-1", "u-3", "u-2")
