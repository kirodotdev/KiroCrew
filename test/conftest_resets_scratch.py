"""Per-test resets of process-global ``kiro_crew.agent_scratch_job`` state, and the
resume of a cron child a test mocked.

Part of ``test/conftest.py``, which imports and re-binds these names so pytest registers
each fixture as the suite conftest's own; see the import block there. Plain module,
never collected.
"""

from __future__ import annotations

import subprocess
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


@pytest.fixture(autouse=True)
def _a_mocked_cron_child_has_no_thread_to_resume(_floor_monkeypatch):
    """On Windows the cron runner creates a script child suspended and resumes its main
    thread. A test that mocks ``Popen`` hands back an object with no thread, so the real
    resume reports a failure and the runner kills the run. Only a real ``Popen`` child is
    resumed for real. POSIX never suspends, so there is nothing to change there."""
    if sys.platform != "win32":
        return
    from kiro_crew import cron_script

    real, popen = cron_script._resume_cron_child, subprocess.Popen  # a test may patch the name
    _floor_monkeypatch.setattr(
        cron_script,
        "_resume_cron_child",
        lambda proc: not isinstance(proc, popen) or real(proc),
    )
