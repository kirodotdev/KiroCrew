"""A repeated query parameter on ``GET /api/work-ledger`` is warned about.

Main reads a parameter's FIRST value, so ``?item_id=A&item_id=B`` filters by
``A`` alone and drops ``B`` with no signal. Repeats stay accepted (a 400 would
newly fail a request main answers); the reply now carries a ``warnings`` entry
naming the parameter, and the route logs it.
"""

from __future__ import annotations

import logging

import pytest
from test_work_ledger_tools import (  # noqa: F401 - the autouse fixtures ride along
    CONDUCTOR_A,
    _clean_slots,
    _isolated_home,
    _open_route,
    _read_with,
    two_by_two,
)


@pytest.mark.asyncio
async def test_a_repeated_filter_keeps_the_first_value_and_warns(caplog):
    ids = await two_by_two()
    with caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.handlers.work_ledger"):
        status, body = await _read_with(CONDUCTOR_A, f"item_id={ids['item_a']}&item_id=it_00000000")

    assert status == 200, body
    # Main's behavior is kept: the first value is the filter.
    assert [row["item_id"] for row in body["items"]] == [ids["item_a"]]
    assert body.get("warnings") == ["item_id was given 2 times; only the first value was used"]
    assert any("item_id" in rec.getMessage() for rec in caplog.records)


@pytest.mark.asyncio
async def test_a_single_filter_carries_no_warnings():
    ids = await two_by_two()
    status, body = await _read_with(CONDUCTOR_A, f"item_id={ids['item_a']}")
    assert status == 200, body
    assert "warnings" not in body
