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


@per_test_guard
def _reset_kas_secret_storage_unknown_kind_warnings():
    """No unknown engine key kind warned about yet: ``kas_secret_storage`` warns once
    per kind per process, so leftover state silences the next test's WARNING."""

    def clear():
        module = sys.modules.get("kiro_crew.acp.kas_secret_storage")
        if module is not None:
            module._WARNED_UNKNOWN_KINDS.clear()

    clear()
    try:
        yield
    finally:
        clear()


@per_test_guard
def _reset_kas_skill_facts():
    """No skill file's facts kept yet: ``kas_skill_scope._skill_facts`` keeps them
    across session starts by path, so a test's files must not answer a later test's."""

    def clear():
        module = sys.modules.get("kiro_crew.acp.kas_skill_scope")
        if module is not None:
            module._SKILL_FACTS.clear()

    clear()
    try:
        yield
    finally:
        clear()
