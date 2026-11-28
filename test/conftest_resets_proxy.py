"""Per-test resets of process-global HTTP proxy selection state.

Part of ``test/conftest.py``, which imports and re-binds these names so pytest registers
each fixture as the suite conftest's own; see the import block there. Plain module,
never collected.
"""

from __future__ import annotations

import sys

from conftest_guards import per_test_guard


def _clear_warned_schemes() -> None:
    # Looked up at test time, never assumed: a tree without the set (the
    # production change reverted) must fail by assertion, not in this fixture.
    warned = getattr(sys.modules.get("kiro_crew.http_proxy"), "_warned_schemes", None)
    if warned is not None:
        warned.clear()


@per_test_guard
def _reset_http_proxy_warned_schemes():
    """Every test starts and ends with no unusable-proxy warning given yet.

    ``http_proxy.resolve_proxy`` warns once per process per proxy scheme it
    cannot use (a Windows ``socks=`` system proxy), so a test that drives that
    warning would otherwise silence it for every later test on the same worker.
    """
    _clear_warned_schemes()
    yield
    _clear_warned_schemes()
