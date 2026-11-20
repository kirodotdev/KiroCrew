"""Per-test resets of process-global ``kiro_crew.agent_scratch_job`` state.

Part of ``test/conftest.py``, which imports and re-binds these names so pytest registers
each fixture as the suite conftest's own; see the import block there. Plain module,
never collected.
"""

from __future__ import annotations

import sys

import pytest


@pytest.fixture(autouse=True)
def _reset_scratch_job_handles():
    """No scratch job held: a script cron run on Windows keeps its job handle until a
    sweep reads the job empty, so a handle a test left would stay open for the worker."""

    def clear():
        module = sys.modules.get("kiro_crew.agent_scratch_job")
        if module is not None:
            held = list(module._JOBS.values())
            module._JOBS.clear()
            for handle in held:
                module._close(handle)

    clear()
    try:
        yield
    finally:
        clear()
