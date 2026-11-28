"""Per-test reset of the kept session-control replies.

Part of ``test/conftest.py``, which imports and re-binds these names so pytest registers
each fixture as the suite conftest's own; see the import block there. Plain module,
never collected.
"""

from __future__ import annotations

import sys

from conftest_guards import per_test_guard


@per_test_guard
def _reset_session_control_replays():
    """Start each test with no kept session-control reply.

    ``dashboard.session_control_replay`` keeps each send's and create's reply by
    caller key and request id, process-wide. Tests reuse both, so a kept reply
    would answer another test's request, or hold a future from a closed loop.
    """

    def clear():
        module = sys.modules.get("kiro_crew.dashboard.session_control_replay")
        if module is not None:
            module.reset_for_tests()

    clear()
    try:
        yield
    finally:
        clear()
