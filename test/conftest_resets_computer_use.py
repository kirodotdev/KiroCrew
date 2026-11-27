"""Per-test resets of process-global ``kiro_crew.computer_use`` state.

Part of ``test/conftest.py``, which imports and re-binds these names so pytest registers
each fixture as the suite conftest's own; see the import block there. Plain module,
never collected.
"""

from __future__ import annotations

import sys

from conftest_guards import per_test_guard


@per_test_guard
def _fresh_computer_use_warning(_floor_monkeypatch):
    """Start every test with no unusable ``computer_use.json`` version warned about.

    ``enable_state.load_state`` warns once per file version, so a test that reads a
    bad keystone would otherwise silence that warning for a later test on the
    same worker.
    """
    from kiro_crew.computer_use import enable_state

    _floor_monkeypatch.setattr(enable_state, "_warned_version", None)


@per_test_guard
def _fresh_computer_use_driver_runtime():
    """Start and end every test with no open-source driver state left by another.

    The shared supervisor keeps the data home it was first built for (and a child),
    ``install.PROGRESS`` the last install's outcome, and ``setup._install_thread`` and
    the setup handler's ``_inflight`` the last action, all process-wide. A test that
    never imported a module pays one dict lookup.
    """

    def clear():
        supervisor = sys.modules.get("kiro_crew.computer_use.driver_runtime.supervisor")
        if supervisor is not None:
            supervisor.reset_shared()
        install = sys.modules.get("kiro_crew.computer_use.driver_runtime.install")
        if install is not None:
            install.PROGRESS = install.InstallProgress()
        setup = sys.modules.get("kiro_crew.computer_use.setup")
        if setup is not None:
            setup._install_thread = None
        handler = sys.modules.get("kiro_crew.dashboard.handlers.computer_use_setup")
        if handler is not None:
            handler._inflight = None

    clear()
    try:
        yield
    finally:
        clear()
