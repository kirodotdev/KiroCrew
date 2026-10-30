"""A rebound worker's binding survives a rebuild of its conductor's board.

A conductor can bind a new item to a worker session whose item it closed, so one
board can hold several items naming the same worker: the terminal ones it
finished and at most one open one. ``rebuild_from_projection`` writes the bindings
back from the record, and must route the worker to its OPEN item whatever order
the items were created in, and must not delete a binding that still points at an
item the record binds to that worker. Driven through the real routes and a real
crew log.
"""

from __future__ import annotations

import pytest
import work_ledger_projection_helpers
from work_ledger_projection_helpers import (
    _SLOTS,
    ACCEPTANCE,
    CONDUCTOR,
    WORKER,
    _real_units,
    _record,
    _report,
    _Slot,
)

from kiro_crew import work_ledger as wl

_isolated = work_ledger_projection_helpers._isolated


async def _create(title: str) -> str:
    status, body = await _record(
        CONDUCTOR, {"action": "create", "title": title, "acceptance": ACCEPTANCE}
    )
    assert status == 200, body
    return body["item"]["item_id"]


async def _ok(body: dict) -> None:
    status, reply = await _record(CONDUCTOR, body)
    assert status == 200, reply


async def _older_then_newer() -> tuple[str, str]:
    """Two items, the OLDER one created first, so the fold lists it first."""
    await _ok({"action": "goal", "goal": "ship it", "round": 1})
    older = await _create("older")
    newer = await _create("newer")
    _SLOTS[WORKER] = _Slot(created_by=CONDUCTOR)
    return older, newer


@pytest.mark.asyncio
async def test_a_rebind_onto_an_older_item_survives_a_rebuild(monkeypatch):
    _real_units(monkeypatch)
    older, newer = await _older_then_newer()
    await _ok({"action": "bind", "item_id": newer, "worker_session_key": WORKER})
    await _ok({"action": "close", "item_id": newer, "state": "accepted"})
    await _ok({"action": "bind", "item_id": older, "worker_session_key": WORKER})
    assert wl.read_binding(WORKER) == (CONDUCTOR, older)

    wl.rebuild_from_projection(CONDUCTOR)

    assert wl.read_binding(WORKER) == (CONDUCTOR, older)
    status, body = await _report(WORKER, {"status": "progress", "summary": "on older"})
    assert status == 200, body
    item = wl.read_work_item(CONDUCTOR, older)
    assert item is not None and item.summary == "on older"


@pytest.mark.asyncio
async def test_a_rebuild_keeps_a_binding_whose_items_are_all_closed(monkeypatch):
    """The worker's last binding points at a terminal item. A rebuild keeps it, so
    the worker still reads ``item_closed`` rather than ``not_bound``."""
    _real_units(monkeypatch)
    older, newer = await _older_then_newer()
    await _ok({"action": "bind", "item_id": newer, "worker_session_key": WORKER})
    await _ok({"action": "close", "item_id": newer, "state": "accepted"})
    await _ok({"action": "bind", "item_id": older, "worker_session_key": WORKER})
    await _ok({"action": "close", "item_id": older, "state": "abandoned"})

    wl.rebuild_from_projection(CONDUCTOR)

    assert wl.read_binding(WORKER) == (CONDUCTOR, older)
    status, body = await _report(WORKER, {"status": "progress", "summary": "late"})
    assert status == 409
    assert body["code"] == wl.CODE_ITEM_CLOSED
