"""The owed wake of an exhausted budget is timed from the day that ran out.

``resource_wakes.ResourceWakeDispatcher`` reads the budget day once and then
times the owed wake to the next UTC midnight. Pinned here: a midnight that falls
between that read and the timing does not defer the wake a further day, so the
new day's budget is used from its start.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from test_mcp_resource_subscription_wake import (  # noqa: F401 -- fixtures ride along
    DAY,
    SLOT,
    _close_windows,
    _FakeClock,
    _ManualSleep,
    _no_active_service,
    _record,
    _service,
    _Turns,
    _updated,
    state,
    turns,
)

from kiro_crew.dashboard import resource_wakes as rw
from kiro_crew.testing.wait import async_wait_until


@pytest.mark.asyncio
async def test_a_midnight_after_the_budget_read_owes_the_wake_now_not_a_day_later(
    tmp_path: Path, state: Any, turns: _Turns  # noqa: F811 -- the imported fixtures
) -> None:
    service = await _service(tmp_path, _record(wakes=rw.WAKES_PER_DAY, day=DAY))
    state.get_or_create_slot(SLOT)
    sleep = _ManualSleep()
    after_midnight = _FakeClock(datetime(2026, 10, 28, 0, 0, 1, tzinfo=timezone.utc))
    dispatcher = rw.ResourceWakeDispatcher(
        service,
        lambda: state,
        today=lambda: DAY,
        sleep=sleep,
        now=after_midnight,
    )

    dispatcher.handle(_updated())
    await _close_windows(dispatcher, sleep)
    await async_wait_until(
        lambda: sleep.waiting() == 1,
        describe=lambda: f"{sleep.waiting()} owed windows reached their sleep",
    )
    assert [secs for secs, future in sleep.calls if not future.done()] == [0.0]
    await dispatcher.close()
