"""The ``sdk`` fixture and the bounded waits the Job SDK test files share.

``sdk`` builds a fresh ``JobSDK`` over ``tmp_path`` and, on teardown, signals and joins
every run still executing before it forgets the SDK, so no worker writes into a removed
tree. ``_wait_terminal`` polls until a run reaches a terminal status and ``_wait_until``
until a predicate holds, each under a bounded deadline. ``test_job_sdk.py`` and
``test_job_sdk_invariants.py`` import all three; ``test_job_sdk_returns.py`` and
``test_job_sdk_params.py`` import the fixture and ``_wait_terminal``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew.apps.job_sdk import JobRun, JobSDK, forget_sdk
from kiro_crew.testing.wait import wait_until


def _wait_terminal(sdk: JobSDK, run_id: str, timeout: float = 5.0) -> JobRun:
    """Poll until the run reaches a terminal status, or fail with what we saw."""

    def _done() -> JobRun | None:
        r = sdk.get(run_id)
        return r if r is not None and r.is_terminal else None

    return wait_until(
        _done,
        timeout=timeout,
        describe=lambda: "status=" + (sdk.get(run_id).status if sdk.get(run_id) else "<no record>"),
    )


def _wait_until(predicate, timeout: float = 5.0) -> None:
    wait_until(predicate, timeout=timeout)


@pytest.fixture(name="sdk")
def sdk_fixture(tmp_path: Path):
    """A fresh SDK over a tmp data dir, unregistered from the global registry
    on teardown so it never leaks into another test in the session.

    Teardown also SIGNALS and WAITS for any run still executing. Runs are real
    daemon threads: one still alive when pytest removes ``tmp_path`` would
    mkdir and write into the deleted tree at its next progress or terminal
    write, which is a real file mutation racing the fixture. Waiting (bounded,
    and loud on timeout) is the only correct answer -- cancelling the wrapper
    would leave the thread running, which is the defect, not the fix.
    """
    s = JobSDK("test-app", tmp_path)
    yield s
    with s._lock:  # noqa: SLF001 - teardown needs the live table the SDK owns
        live = list(s._live.values())
    for entry in live:
        entry.handle.discarded.set()
        entry.handle.cancelled.set()
    for entry in live:
        entry.thread.join(timeout=5.0)
        assert not entry.thread.is_alive(), (
            "a job worker outlived its test and would write into the removed "
            f"tmp_path: {entry.thread.name}"
        )
    forget_sdk(s.app_name)
