"""Count the work one slot-list broadcast does when a single row changed.

At 2,000 seeded slots, one mutation of one row followed by one flush of the slot
list re-serializes every row today: ``serialize_slot`` runs once per slot for the
bare list, and the dashboard-user and owner views re-project only
``source_links``. The budgets are call counts, never wall time, so host speed
cannot fail them.

- ``serialize_slot`` is at most 3N per flush: one pass per audience view at most.
  A change that makes a flush serialize the list once per socket, or once per
  view, crosses it.
- ``serialize_slot`` is at most 10 for one dirty row. That is the target of a
  per-slot dirty set with cached row text, and it is a strict xfail until that
  change lands and deletes the marker.
- ``json.dumps`` from dashboard code is at most 3 per dirty row, plus one per
  envelope field, plus one per frame sent. This holds today and keeps a later
  change from dumping every row once per view.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from chat_runner_coverage_helpers import _state
from slot_scale_helpers import seed_key, seed_slots
from turn_golden_harness import SocketDouble, _SteppingClock

from kiro_crew.dashboard import state as dashboard_state
from kiro_crew.dashboard.state import DashboardState

N_SLOTS = 2_000

#: The frame types one slot-list flush can send. The slots frame is the only one
#: today; a diff or patch frame added later counts against the same budget.
_SLOT_FRAMES = frozenset({"slots", "slot_patch", "slots_diff"})


class _Counts:
    def __init__(self) -> None:
        self.serialize_slot = 0
        self.dumps = 0


def _dashboard_caller(depth: int = 2) -> bool:
    """Whether the ``json.dumps`` caller is dashboard code, not an audit sink."""
    filename = sys._getframe(depth).f_code.co_filename
    return f"{Path('kiro_crew') / 'dashboard'}" in filename


async def _drain() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


async def _one_row_flush(tmp_path: Path) -> tuple[_Counts, list[dict]]:
    """Seed ``N_SLOTS`` slots, change one row's title, flush once, and count.

    The stepping broadcast clock makes every push a leading-edge broadcast, so the
    flush happens inside the counted window and no trailing timer is left armed.
    """
    state = _state(tmp_path)
    # The coverage builder stubs the push path; this test needs the real one.
    for name in ("broadcast_ws", "push_slots_update", "push_refresh"):
        state.__dict__.pop(name, None)
    viewer = SocketDouble()
    state._ws_clients.append(viewer)
    counts = _Counts()
    real_serialize = DashboardState.serialize_slot
    real_dumps = json.dumps
    counting = False

    def serialize_spy(self, *args, **kwargs):
        if counting:
            counts.serialize_slot += 1
        return real_serialize(self, *args, **kwargs)

    def dumps_spy(*args, **kwargs):
        if counting and _dashboard_caller():
            counts.dumps += 1
        return real_dumps(*args, **kwargs)

    with (
        patch.object(dashboard_state, "_slots_broadcast_clock", _SteppingClock()),
        patch.object(DashboardState, "serialize_slot", serialize_spy),
        patch("json.dumps", dumps_spy),
    ):
        slots = seed_slots(state, N_SLOTS)
        await _drain()
        viewer.frames.clear()
        counting = True
        slots[N_SLOTS // 2].title = "Renamed session"
        state.push_slots_update()
        counting = False
        await _drain()
    assert state._slots_broadcast_timer is None, "the flush was deferred to a timer"
    frames = [f for f in viewer.frames if f.get("type") in _SLOT_FRAMES]
    return counts, frames


@pytest.mark.asyncio
async def test_seeding_costs_one_pass(tmp_path: Path) -> None:
    """``seed_slots`` serializes each row once, not 1 + 2 + ... + N times."""
    state = _state(tmp_path)
    state.__dict__.pop("push_slots_update", None)
    calls = 0
    real = DashboardState.serialize_slot

    def spy(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        return real(self, *args, **kwargs)

    # The stepping clock makes every push a leading-edge broadcast, the case in
    # which per-slot creation pushes cost 1 + 2 + ... + N rows. On the real clock
    # the coalescing window would absorb most of them and hide the regression.
    with (
        patch.object(dashboard_state, "_slots_broadcast_clock", _SteppingClock()),
        patch.object(DashboardState, "serialize_slot", spy),
    ):
        slots = seed_slots(state, 300)
    assert len(slots) == 300 and state._slots[seed_key(299)] is slots[-1]
    assert calls <= 3 * 300, f"seeding 300 slots ran {calls} serialize_slot calls"


@pytest.mark.asyncio
async def test_one_row_flush_serializes_at_most_three_passes(tmp_path: Path) -> None:
    counts, frames = await _one_row_flush(tmp_path)
    assert frames, "the flush sent no slot-list frame, so nothing was measured"
    assert counts.serialize_slot <= 3 * N_SLOTS, (
        f"one flush at {N_SLOTS} slots ran {counts.serialize_slot} serialize_slot calls, "
        f"more than one pass per audience view ({3 * N_SLOTS})"
    )


@pytest.mark.asyncio
async def test_one_row_flush_dumps_budget(tmp_path: Path) -> None:
    counts, frames = await _one_row_flush(tmp_path)
    envelope_fields = max(len(frame) - 1 for frame in frames)
    budget = 3 * 1 + envelope_fields + len(frames)
    assert counts.dumps <= budget, (
        f"one flush ran {counts.dumps} json.dumps calls from dashboard code; the budget is "
        f"3 per dirty row + {envelope_fields} envelope fields + {len(frames)} frame(s) = {budget}"
    )


@pytest.mark.asyncio
@pytest.mark.xfail(strict=True, reason="scale-F19: one dirty slot still re-serializes every row")
async def test_one_dirty_row_serializes_at_most_ten_rows(tmp_path: Path) -> None:
    counts, _frames = await _one_row_flush(tmp_path)
    assert (
        counts.serialize_slot <= 10
    ), f"one dirty row at {N_SLOTS} slots ran {counts.serialize_slot} serialize_slot calls"
