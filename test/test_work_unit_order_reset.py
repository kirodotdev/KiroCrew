"""A reset's new unit is listed LAST, so the work, panel and ledger folds stay warm.

``work_crew_log_units``, ``panel_crew_log_units`` and the ledger's ``crew_log_units`` list
units missing from the order file before the recorded ones, except a unit newer than every
recorded one, which goes after them. A conductor reset opens such a unit, so its open and its first record are
appends, and the warm cell carries over both instead of refolding the slot cold.

Each test runs the real crew-log writer on a temp data home and checks the warm answer
against a from-scratch fold of the same units.
"""

from __future__ import annotations

import itertools
import sys
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


@pytest.fixture(autouse=True)
def _ordered_clock(_floor_monkeypatch):
    """Each header stamp one second after the last, so creation order never ties.

    Two units created within one clock tick would tie on ``createdAt``, and the store
    then orders them by id, which is not creation order for ids like ``new-20`` and
    ``old-20``. Pinned at the binding the header writer reads.
    """
    from kiro_crew.crew_log import store as crew_log_store

    ticks = itertools.count(1_700_000_000_000, 1_000)
    _floor_monkeypatch.setattr(
        crew_log_store, "now_ms", lambda: next(ticks)  # flake-ok: a fixed clock pin
    )


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
    elif name == "ledger":
        data = {"slot": SLOT, "next": f"step {_seq[0]}", "event": f"e{_seq[0]}"}
        data["event_kind"] = "progress"
        _handles[unit].append("ledger/recorded", data, src="gateway")
        # What ``record_update`` publishes once the append has landed.
        session_ledger_control._note_unit_order(SLOT, unit)
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


@pytest.mark.parametrize("name", ["work", "panel", "ledger"])
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


@pytest.mark.parametrize("name", ["work", "panel", "ledger"])
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


def test_the_ledger_writer_still_folds_its_own_unit_last(origins):
    """The live unit is last on the writer's own call, whatever the order log says."""
    _two_units("ledger", origins)
    _open("u-3")
    assert session_ledger.crew_log_units(SLOT) == ("u-1", "u-2", "u-3")
    assert session_ledger.crew_log_units(SLOT, "u-1") == ("u-2", "u-3", "u-1")


def _restart_cost(entries: int) -> "tuple[int, bool]":
    """Entries read by a ledger read after a restart, once a reset's unit appended one.

    The old unit records *entries* ledger entries; a reset opens a new unit that writes
    its own traffic but no ledger entry yet. A read folds and leaves a savepoint; the new
    unit appends once; the warm table is cleared (a restart) and the slot is read again
    through the real read path. Counted with a wrapper around ``CrewLog.iter_from``.
    """
    old, new = f"old-{entries}", f"new-{entries}"
    _open(old)
    for _ in range(entries):
        _record("ledger", old)
    _open(new)
    _handles[new].append("turn/started", {"turn": 1, "actor": "user", "depth": 0}, src="gateway")
    units = _slot_units_for_fold(SLOT, "ledger")
    crew_log.fold_slot_warm("ledger", units, slot=SLOT)
    _handles[new].append("turn/started", {"turn": 2, "actor": "user", "depth": 0}, src="gateway")
    crew_log.forget_slot_folds()
    read = {"entries": 0}
    real = CrewLog.iter_from

    def counted(self, *args, **kwargs):
        for entry in real(self, *args, **kwargs):
            read["entries"] += 1
            yield entry

    CrewLog.iter_from = counted  # type: ignore[method-assign]
    try:
        units = _slot_units_for_fold(SLOT, "ledger")
        warm = crew_log.fold_slot_warm("ledger", units, slot=SLOT).state
    finally:
        CrewLog.iter_from = real  # type: ignore[method-assign]
    cold = crew_log.fold_slot_checkpoint("ledger", units, slot=SLOT).state
    return read["entries"], warm == cold and units[-1] == new


def test_a_restart_after_a_resets_own_append_reads_only_the_append(monkeypatch):
    """Flat cost: the read after a restart does not grow with the old unit's history.

    Red before the fix: the ledger list put the reset's unrecorded unit FIRST, so its own
    append retired the savepoint and the restart folded every entry of the old unit.
    """
    from kiro_crew.crew_log import checkpoint as savepoints

    monkeypatch.setattr(savepoints, "MIN_ADVANCE_ENTRIES", 2)
    costs = []
    for entries in (20, 80, 320):
        # One slot per size, so no size reads another's units.
        monkeypatch.setattr(sys.modules[__name__], "SLOT", f"member-order-cost-{entries}")
        costs.append(_restart_cost(entries))
    assert len({cost for cost, _same in costs}) == 1 and costs[0][0] <= 2, costs
    assert all(same for _cost, same in costs), "the resumed value is the cold value"
