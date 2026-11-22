"""Per-test resets of process-global ``kiro_crew.acp`` state.

Part of ``test/conftest.py``, which imports and re-binds these names so pytest registers
each fixture as the suite conftest's own; see the import block there. Plain module,
never collected.
"""

from __future__ import annotations

import sys

from conftest_guards import per_test_guard


@per_test_guard
def _reset_kas_agent_config_error_reports():
    """No agent config error reported yet: ``kas_agent_config_error.report`` warns
    once per report and once at its cap, so leftover state silences the next WARNING."""

    def clear():
        module = sys.modules.get("kiro_crew.acp.kas_agent_config_error")
        if module is not None:
            module._reported.clear()
            module._cap_warned = False

    clear()
    try:
        yield
    finally:
        clear()
