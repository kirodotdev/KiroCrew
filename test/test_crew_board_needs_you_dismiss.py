"""A "needs you" ask clears once its worker moves on.

``work_ledger_board.awaits_person``: a worker whose turn runs again (the conductor
messaged it, a loop woke it) has moved on, though nothing wrote a ledger answer, so
the ask is not the person's.

Reuses the two-conductor fixture of ``test_work_ledger_tools``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from test_work_ledger_tools import (  # noqa: F401 - the autouse fixtures ride along
    CONDUCTOR_A,
    WORKER_A,
    _clean_slots,
    _isolated_home,
    _open_route,
    _report,
    two_by_two,
)

from kiro_crew import work_ledger as wl
from kiro_crew.dashboard.handlers import work_ledger_board as board

OWNER = "owner-subject"


def _state(*, running: bool):
    slots = {WORKER_A: SimpleNamespace(running=running)}
    return SimpleNamespace(get_slot=slots.get, owner_id=OWNER)


def _rows(item_id: str) -> list[dict]:
    item = wl.read_work_item(CONDUCTOR_A, item_id)
    assert item is not None
    events = [e.to_dict() for e in wl.read_events(CONDUCTOR_A, item_id)]
    return [{**item.to_dict(), "events": events}]


async def _asks(text: str = "Pick a region.") -> None:
    status, _ = await _report(
        WORKER_A, {"status": "question", "summary": text, "reason": "needs_human"}
    )
    assert status == 200


@pytest.mark.asyncio
async def test_a_worker_running_again_no_longer_needs_the_person():
    ids = await two_by_two()
    await _asks()
    rows = _rows(ids["item_a"])

    assert board.with_worker_facts(rows, _state(running=False))[0]["awaits_person"] is True
    # The conductor messaged the worker: its turn runs, no ledger answer was written.
    assert board.with_worker_facts(rows, _state(running=True))[0]["awaits_person"] is False
