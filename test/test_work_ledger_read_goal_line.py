# SPDX-License-Identifier: Apache-2.0
"""``work_ledger_read``'s goal on a compact or one-item read: its first line only.

A patrol re-reads the board every cycle and a wake re-reads one item, so neither
carries the whole goal; the plain read keeps it whole.
"""

from __future__ import annotations

import pytest
from test_work_ledger_tools import (  # noqa: F401 - the autouse fixtures apply here too
    CONDUCTOR_A,
    _clean_slots,
    _isolated_home,
    _open_route,
    _read,
    _read_with,
    _record,
    two_by_two,
)


@pytest.mark.asyncio
async def test_a_compact_or_one_item_read_carries_the_goals_first_line_only():
    """A patrol re-reads the board every cycle; the whole goal rides the plain read."""
    await two_by_two()
    goal = "Ship the importer\n" + "detail " * 150
    status, _ = await _record(CONDUCTOR_A, {"action": "goal", "goal": goal})
    assert status == 200
    status, full = await _read(CONDUCTOR_A)
    assert full["conductor"]["goal"] == goal.strip()
    item_id = full["items"][0]["item_id"]
    for query in ("compact=true", f"item_id={item_id}"):
        status, read = await _read_with(CONDUCTOR_A, query)
        assert status == 200
        assert read["conductor"]["goal"] == "Ship the importer…", query
        assert read["conductor"]["goal_version"] == full["conductor"]["goal_version"]
    status, _ = await _record(CONDUCTOR_A, {"action": "goal", "goal": "x" * 500})
    status, read = await _read_with(CONDUCTOR_A, "compact=true")
    assert read["conductor"]["goal"] == "x" * 199 + "…"
    # A filter that is neither keeps the whole goal.
    status, read = await _read_with(CONDUCTOR_A, "state=open")
    assert read["conductor"]["goal"] == "x" * 500
