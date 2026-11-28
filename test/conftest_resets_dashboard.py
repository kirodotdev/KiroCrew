"""Per-test resets of process-global ``kiro_crew.dashboard`` state.

The suite conftest imports and re-binds these names so pytest registers each fixture as
its own. Plain module, never collected.
"""

from __future__ import annotations

import sys

from conftest_guards import per_test_guard


@per_test_guard
def _no_full_chat_lapse_timers():
    """No lapse timer left: ``full_chat_waits._place`` keeps the earlier of two timers,
    so one an earlier test left on a state would decide this test's timer."""

    def clear():
        module = sys.modules.get("kiro_crew.dashboard.full_chat_waits")
        if module is not None:
            for _loop, timer in list(module._LAPSE_TIMERS.values()):
                timer.cancel()
            module._LAPSE_TIMERS.clear()

    clear()
    try:
        yield
    finally:
        clear()
