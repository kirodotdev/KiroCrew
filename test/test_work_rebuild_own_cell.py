"""The work-ledger rebuild folds a LONGER unit list than every other work reader, so it
must not use their cell or write their savepoint files.

``work_ledger.rebuild_from_projection`` reads the board with ``also_slots`` (its bound
workers' units appended after its own). Every other reader of the board's ``work`` cell
lists the board's own units only. Sharing one cell meant two things after a rebuild:
the next ordinary read found a memo for the longer list and resumed from disk instead
of continuing it, and the rebuild's savepoint landed beside its list's newest unit -- a
WORKER's unit -- over that worker's own ``work.json``.

Real store and read path on a temp data home; disk reads are counted with a wrapper
around ``CrewLog.iter_from``.
"""

from __future__ import annotations

import itertools
from typing import Any

import pytest

from kiro_crew import crew_log as lg
from kiro_crew import session_ledger
from kiro_crew.crew_log import CrewLog
from kiro_crew.crew_log import checkpoint as savepoints
from kiro_crew.crew_log import eager as crew_log_eager
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.crew_log import store as crew_log_store

BOARD, WORKER = "chat-rebuild-board", "chat-rebuild-worker"
BOARD_UNIT, WORKER_UNIT = "u-rebuild-board", "u-rebuild-worker"


@pytest.fixture(autouse=True)
def _home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    _floor_monkeypatch.setenv("KIROCREW_CREW_LOG", "1")
    _floor_monkeypatch.setattr(crew_log_eager, "note_commit", lambda *a, **k: None)
    _floor_monkeypatch.setattr(savepoints, "MIN_ADVANCE_ENTRIES", 2)
    ticks = itertools.count(1_700_000_000_000, 1_000)
    _floor_monkeypatch.setattr(
        crew_log_store, "now_ms", lambda: next(ticks)  # flake-ok: a fixed clock pin
    )
    crew_log_emit.reset_caches()
    crew_log_eager.stop_for_tests()
    crew_log.forget_slot_folds()
    yield
    for handle in _handles.values():
        handle.release_ownership()
    _handles.clear()
    crew_log.forget_slot_folds()
    crew_log_emit.reset_caches()


_handles: "dict[str, CrewLog]" = {}


def _open(unit: str, slot: str) -> None:
    _handles[unit] = CrewLog.create(lg.KIND_SESSION, unit, owner="owner", agent="lead", slot=slot)


def _work(unit: str, owner_slot: str, **fields: Any) -> None:
    data = {"slot": BOARD, "by": BOARD, **fields}
    _handles[unit].append("work/recorded", data, src="gateway")
    session_ledger.note_work_unit_recorded(owner_slot, unit)


def _board_and_worker() -> None:
    _open(BOARD_UNIT, BOARD)
    for n in range(4):
        _work(
            BOARD_UNIT, BOARD, actor="conductor", action="create", item_id=f"it_{n}", title=f"t{n}"
        )
    _open(WORKER_UNIT, WORKER)
    for n in range(3):
        _work(WORKER_UNIT, WORKER, actor="worker", action="report", item_id="it_0", summary=f"s{n}")


def _counted(read: Any) -> "tuple[Any, int]":
    seen = {"entries": 0}
    real = CrewLog.iter_from

    def counted(self, *args, **kwargs):
        for entry in real(self, *args, **kwargs):
            seen["entries"] += 1
            yield entry

    CrewLog.iter_from = counted  # type: ignore[method-assign]
    try:
        value = read()
    finally:
        CrewLog.iter_from = real  # type: ignore[method-assign]
    return value, seen["entries"]


def _worker_savepoint() -> "bytes | None":
    path = savepoints.slot_checkpoint_path(lg.KIND_SESSION, WORKER_UNIT, "work")
    return path.read_bytes() if path.is_file() else None


def test_the_next_read_after_a_rebuild_reads_nothing_from_disk():
    _board_and_worker()
    crew_log.read_slot_projection(BOARD, "work")
    # One more entry, folded warm: below the write threshold, so the cell is ahead of
    # its savepoint, which is the ordinary state of a live board between writes.
    _work(BOARD_UNIT, BOARD, actor="conductor", action="create", item_id="it_late", title="late")
    before = crew_log.read_slot_projection(BOARD, "work").value

    rebuild = crew_log.read_slot_projection(BOARD, "work", also_slots=(WORKER,))
    rebuilt = rebuild.value
    shared, read = _counted(lambda: crew_log.read_slot_projection(BOARD, "work"))
    after = shared.value
    assert rebuild.revision <= shared.revision, "the one-off value never outranks the cell"

    assert read == 0, "the board's own cell is still warm after the rebuild"
    assert after == before
    units = (BOARD_UNIT, WORKER_UNIT)
    assert rebuilt == crew_log.fold_slot("work", units, slot=BOARD).value, "a cold fold's value"


def test_a_rebuild_leaves_the_workers_own_savepoint_alone():
    _board_and_worker()
    crew_log.read_slot_projection(WORKER, "work")
    held = _worker_savepoint()

    crew_log.read_slot_projection(BOARD, "work", also_slots=(WORKER,))

    assert _worker_savepoint() == held, "the rebuild wrote nothing beside the worker's unit"


def test_a_rebuild_on_the_request_path_answers_not_ready():
    """``cold=False`` never pays for a fold of every unit, the rebuild's included."""
    _board_and_worker()
    from kiro_crew.crew_log.projection_slot_memo import SlotFoldNotReady

    with pytest.raises(SlotFoldNotReady):
        crew_log.read_slot_projection(BOARD, "work", also_slots=(WORKER,), cold=False)


def test_a_read_whose_extra_slots_add_nothing_uses_the_shared_cell():
    """An ``also_slots`` that adds no unit is the ordinary list, and the ordinary cell."""
    _board_and_worker()
    crew_log.read_slot_projection(BOARD, "work")
    _value, read = _counted(
        lambda: crew_log.read_slot_projection(BOARD, "work", also_slots=("chat-nobody",))
    )
    assert read == 0
