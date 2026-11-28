"""Per-test resets of process-global ``kiro_crew.connected_computers`` state.

Part of ``test/conftest.py``, which imports and re-binds these names so pytest registers
each fixture as the suite conftest's own; see the import block there. Plain module,
never collected.
"""

from __future__ import annotations

import sys

import pytest


@pytest.fixture(autouse=True)
def _reset_connected_computers_state():
    """No controller or task registry survives its test's data home.

    Only inspect loaded modules. The root task-drain floor owns awaiting live
    tasks before the loop closes; these resets discard their retained handles.
    Static protocol/syscall tables are not runtime caches and stay unchanged.
    """
    from contextvars import ContextVar

    def clear():
        for name, attribute, factory in (
            ("broker", "_BROKER", lambda: None),
            ("controller", "_CONTROLLER", lambda: None),
            ("disable", "_STARTUP_TASK", lambda: None),
            ("disable", "_TASKS", set),
            ("connector", "_LATE_STOPS", set),
            ("launch", "_WATCHERS", set),
            (
                "permission_audit",
                "_caller",
                lambda: ContextVar(
                    "connected_computer_permission_caller", default="connected_computers"
                ),
            ),
        ):
            module = sys.modules.get("kiro_crew.connected_computers." + name)
            if module is not None:
                setattr(module, attribute, factory())

    clear()
    try:
        yield
    finally:
        clear()
