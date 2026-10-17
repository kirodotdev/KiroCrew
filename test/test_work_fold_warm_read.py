"""The work board is read WARM: a second read folds no entry again.

This is the O(1)-amortized claim, MEASURED rather than asserted. The provider takes its
input from ``read_slot_projection(slot, "work")``, which routes to ``fold_slot_warm`` --
whose own contract is that a warm read continues from its remembered position while a
cold read streams every unit. A provider that re-folded the whole log on every drawer
open would still be correct and still return :class:`WorkBoardView`, so no type and no
parity gate can tell the two apart. Only a count can.

The measurement counts ``CrewLog.iter_from`` calls, which is where entries are actually
streamed off disk, and it is taken in two parts so the assertion says what it means: the
whole second read, and then unit DISCOVERY on its own. Discovery is the scan that finds
which units name this board; it is not folding and it happens on every read by design.
Subtracting it is what turns "the second read still touched files" into "the second read
folded nothing again".

:func:`test_a_cold_read_does_stream_the_log` is the control. Without it, a measurement of
zero folding work would also be produced by a fold that never read anything at all --
which is the failure mode that reads exactly like success.
"""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog
from kiro_crew.crew_log import eager as crew_log_eager
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.work_vocab import WorkBoardView

SLOT = "member-fleet-crew"
UNIT = "acp-fleet-crew"
CREW = "fleet-crew"
WORKER = "chat-4242-worker"
WORK_FOLD = "work"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Own data home, crew log on, and no warm fold carried between tests.

    The eager folder is silenced here, and that is what makes the measurement below mean
    anything. Left running, committing a ``work/recorded`` wakes a background thread that
    folds this same log through this same warm path -- so its ``iter_from`` calls land in
    the counter, and the memo moves under the read being measured. This file measures what
    a READ costs, so the only other folder of this log is stopped for it. The lazy contract
    it pins is unchanged either way: it is the one the read path honours whenever no wake
    arrived, which is every fold that is not eager and every process that did not write the
    entry.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KIROCREW_CREW_LOG", "1")
    monkeypatch.setattr(crew_log_eager, "note_commit", lambda *a, **k: None)
    crew_log_emit.reset_caches()
    crew_log_eager.stop_for_tests()
    crew_log.forget_slot_folds()
    yield
    crew_log_emit.reset_caches()
    crew_log_eager.stop_for_tests()
    crew_log.forget_slot_folds()


def _unit() -> None:
    CrewLog.create(lg.KIND_SESSION, UNIT, owner="owner", agent=CREW, slot=SLOT)


def _work(*, action: str, **fields: Any) -> None:
    """One ``work/recorded`` entry on this slot's board, appended AND acknowledged.

    The append must LAND: the emitter answers ``False`` on a refused or abandoned one,
    and a silently missing entry makes the board smaller and the fold cheaper -- exactly
    the direction this file measures, so an unchecked append would manufacture the
    result. ``slot``/``actor``/``by`` are what the entry validator requires; without them
    every append is refused and the board comes back empty, which reads as a very warm
    read.
    """
    payload: dict[str, Any] = {"slot": SLOT, "actor": "conductor", "by": SLOT, "action": action}
    payload.update({name: value for name, value in fields.items() if value is not None})
    landed = crew_log_emit.on_work_recorded(UNIT, payload, timeout=5.0)
    assert landed is True, f"append refused: {payload}"
    crew_log_emit.flush(timeout=5.0)


def _board(items: int) -> None:
    """A goal plus *items* created items, each bound to a worker."""
    _work(action="goal", goal="ship the board", round=3)
    for n in range(items):
        item_id = f"it_{n:08x}"
        _work(
            action="create",
            item_id=item_id,
            title=f"item {n}",
            acceptance={"kind": "human_approval"},
            round=3,
        )
        _work(action="bind", item_id=item_id, worker_session_key=f"{WORKER}-{n}")


def _view() -> WorkBoardView:
    """The board through the provider's own read path."""
    value = crew_log.read_slot_projection(SLOT, WORK_FOLD).value
    assert isinstance(value, dict)
    return value  # type: ignore[return-value]


def _count_iter_from(monkeypatch) -> list[int]:
    """Start offsets of every entry stream, in call order."""
    seen: list[int] = []
    real = CrewLog.iter_from

    def counted(self, start, **kwargs):
        seen.append(start)
        return real(self, start, **kwargs)

    monkeypatch.setattr(CrewLog, "iter_from", counted)
    return seen


def test_a_cold_read_does_stream_the_log(monkeypatch) -> None:
    """The control. A measurement of "no folding" is worthless without this.

    A fold that read nothing at all, or a board that was never written, produces the
    same zero as a perfectly warm read. This proves the counter observes real work
    before the next test reads its absence as evidence.
    """
    _unit()
    _board(items=2)
    crew_log.forget_slot_folds()
    reads = _count_iter_from(monkeypatch)
    view = _view()
    assert len(view["items"]) == 2, "the board did not materialise; the control proves nothing"
    assert reads, "a cold read streamed no entries -- the counter is not observing the fold"


def test_the_second_read_folds_no_entry_again(monkeypatch) -> None:
    """THE claim: the residue of a second read is unit discovery, not folding."""
    _unit()
    _board(items=2)
    first = _view()
    assert len(first["items"]) == 2

    reads = _count_iter_from(monkeypatch)
    second = _view()
    whole = list(reads)

    reads.clear()
    crew_log._slot_units_for_fold(SLOT, WORK_FOLD)
    discovery = list(reads)

    assert len(whole) == len(discovery), (
        f"a second read made {len(whole)} stream(s) where discovery alone makes "
        f"{len(discovery)}; the difference is the log being folded again"
    )
    assert second == first, "the warm read returned a different board than the cold one"


def test_a_new_entry_is_folded_from_the_remembered_position(monkeypatch) -> None:
    """Warm does not mean stale: a third item appears, and only it is folded.

    The other half of the contract, and the one a naive cache would fail. Without it,
    "the second read folds nothing" is also satisfied by a read that can never see an
    update.
    """
    _unit()
    _board(items=2)
    assert len(_view()["items"]) == 2

    _work(
        action="create",
        item_id="it_0000000a",
        title="item 2",
        acceptance={"kind": "human_approval"},
        round=3,
    )
    reads = _count_iter_from(monkeypatch)
    after = _view()
    whole = list(reads)
    reads.clear()
    crew_log._slot_units_for_fold(SLOT, WORK_FOLD)
    discovery = list(reads)

    assert len(after["items"]) == 3, "the warm read did not see the new item"
    assert len(whole) == len(discovery) + 1, (
        f"expected exactly one fold stream for the new entry; made {len(whole)} with "
        f"{len(discovery)} of discovery"
    )
