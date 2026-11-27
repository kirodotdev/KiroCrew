"""Per-test resets of process-global remote-dispatch state.

Part of ``test/conftest.py``, which imports and re-binds these names so pytest registers
each fixture as the suite conftest's own; see the import block there. Plain module,
never collected.
"""

from __future__ import annotations

import sys

from conftest_guards import per_test_guard


@per_test_guard
def _forget_remote_crew_versions():
    """Give every test an empty remote-dispatch version record and warn-once set.

    ``remote_dispatch_routes.check_version`` records each crew's version in
    ``_PEER_VERSIONS`` and warns once per pair in ``_SKEW_WARNED``. Both are
    process-global, so a test that reads a skewed crew would silence the warning,
    and name its version, for a later test on the same worker.
    """

    def _clear() -> None:
        # Read from sys.modules, never imported here: a test pins that the route
        # table does not load this module at import.
        mod = sys.modules.get("kiro_crew.dashboard.handlers.remote_dispatch_routes")
        if mod is not None:
            mod._PEER_VERSIONS.clear()
            mod._SKEW_WARNED.clear()

    _clear()
    yield
    _clear()
