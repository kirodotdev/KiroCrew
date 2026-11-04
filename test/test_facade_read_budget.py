"""Snapshot ceilings on facade-mirror reads per slot in two per-slot loops.

A module attribute read through a facade (``members_mod.DM_SLOT_MODE``) costs a
``MirroredModule.__getattr__`` call, about twenty times a plain global read, and a
read inside a per-slot loop pays it once per row: at the scale bed's shape it was
the second-hottest self frame by GIL time. These tests count those calls for one
crewmate scan and one slots flush at 200 and at 400 slots and hold the difference,
the reads each extra row costs, to the number measured on the current main. A new
per-row facade read raises it by one and fails; lowering it (a hoist) is welcome,
lower the ceiling with it.

The per-row count, not the total, is the snapshot: a flush also pays a few reads
once (lazy imports, config lookups), and how many depends on the platform and on
what earlier tests on the worker imported, so a total is not the same number on
every runner.
"""

from __future__ import annotations

import json
from collections import Counter
from unittest.mock import AsyncMock

import pytest
from dashboard_state_ws_helpers import (  # noqa: F401 -- fixtures are requested by name
    state_fixture,
    sync_event_loop,
)

from kiro_crew import _facade_mirror
from kiro_crew.dashboard import crewmate_feed
from kiro_crew.dashboard.state import DashboardState

STEP = 200

# Facade reads each extra slot costs, measured on the main this file was written
# against. The flush reads 6 per row: two ``__path__`` lookups from a per-row
# import, and one each of ``effective_session_key``, ``chunk_generation``,
# ``mirror_is_paused`` and ``slack_mirror_is_paused``.
CREWMATE_SCAN_READS_PER_SLOT = 0
SLOTS_FLUSH_READS_PER_SLOT = 6


def seed_slots(state: DashboardState, n: int, *, start: int = 0) -> None:
    """Create *n* slots with one broadcast at the end, not one per slot.

    Plain ``get_or_create_slot`` broadcasts after every slot, which re-serializes
    the whole list each time: quadratic in *n*.
    """
    with state.suspend_slots_push():
        for i in range(start, start + n):
            state.get_or_create_slot(f"chat-budget-{i:04d}")


class _DashboardWS:
    """A connected dashboard-user socket whose send is recorded, not written."""

    def __init__(self) -> None:
        self.closed = False
        self.send_str = AsyncMock()
        self._flags = {"_is_dashboard_user": True}

    def get(self, key, default=None):
        return self._flags.get(key, default)


@pytest.fixture
def facade_reads(monkeypatch: pytest.MonkeyPatch) -> Counter:
    """Count ``MirroredModule.__getattr__`` calls by attribute name."""
    seen: Counter = Counter()
    real = _facade_mirror.MirroredModule.__getattr__

    def counting(self, name: str):
        seen[name] += 1
        return real(self, name)

    monkeypatch.setattr(_facade_mirror.MirroredModule, "__getattr__", counting)
    return seen


def _reads_of(facade_reads: Counter, run) -> Counter:
    """The facade reads one warm call of *run* makes."""
    run()  # warm: one-time lazy imports are not per-row cost
    facade_reads.clear()
    run()
    return Counter(facade_reads)


def _per_slot(small: Counter, large: Counter) -> tuple[float, list[tuple[str, int]]]:
    grown = large - small
    return sum(grown.values()) / STEP, sorted(grown.items())


def test_one_crewmate_scan_reads_no_facade_per_slot(
    state: DashboardState, facade_reads: Counter
) -> None:
    def scan() -> None:
        crewmate_feed._crewmate_slots(state)

    seed_slots(state, STEP)
    small = _reads_of(facade_reads, scan)
    seed_slots(state, STEP, start=STEP)
    large = _reads_of(facade_reads, scan)

    per_slot, grown = _per_slot(small, large)
    assert per_slot <= CREWMATE_SCAN_READS_PER_SLOT, (
        f"one crewmate scan costs {per_slot} facade reads per slot "
        f"(ceiling {CREWMATE_SCAN_READS_PER_SLOT}); reads added by {STEP} slots: {grown}"
    )


def test_one_slots_flush_stays_under_its_per_slot_facade_read_ceiling(
    state: DashboardState, facade_reads: Counter
) -> None:
    ws = _DashboardWS()
    state.register_ws(ws)  # type: ignore[arg-type]

    def flush() -> None:
        ws.send_str.reset_mock()
        state._do_slots_broadcast()

    seed_slots(state, STEP)
    small = _reads_of(facade_reads, flush)
    seed_slots(state, STEP, start=STEP)
    large = _reads_of(facade_reads, flush)

    frame = json.loads(ws.send_str.call_args[0][0])
    assert frame["type"] == "slots" and len(frame["data"]) == 2 * STEP
    per_slot, grown = _per_slot(small, large)
    assert per_slot <= SLOTS_FLUSH_READS_PER_SLOT, (
        f"one slots flush costs {per_slot} facade reads per slot "
        f"(ceiling {SLOTS_FLUSH_READS_PER_SLOT}); reads added by {STEP} slots: {grown}"
    )
