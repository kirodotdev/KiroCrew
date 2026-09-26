"""The supervisor's exit code is the only signal the platform reads.

``run()`` ended with an unconditional ``return 0`` after teardown, so a task whose
backend had crashed reported the same status as one ECS had asked to stop. On the
console that is a task exiting normally, over and over, with nothing marked failed --
the crash loop is invisible exactly when it matters.

Nothing asserted the return value before, which is how it survived: the whole
supervisor suite passed with the bug in place.

An orderly stop has a second half. The sidecar's post-shutdown cycle runs after the
backend's flush and carries the only copy of the turns in it, so its status belongs in
the exit code too: a task signalled to stop, failing that cycle and still reporting 0,
hands the platform a lossless replacement for a lossy one.
"""

from __future__ import annotations

import pytest
from container.supervisor import __main__ as entry

from .test_supervisor_main import make_settings, wired  # noqa: F401  (pytest fixture)


def test_an_orderly_signal_is_success(wired, tmp_path):  # noqa: F811
    rc = entry.run(make_settings(tmp_path, bucket=None), wait_for_shutdown=lambda c: "signal")
    assert rc == 0


def test_a_spent_lifetime_is_success(wired, tmp_path):  # noqa: F811
    """The bound working is not an incident.

    A task stopped by its own deadline ran for as long as it was allowed and then
    stood down. Reporting that as a failure would put an expiry beside a crash loop
    on the console and leave an operator reading every one of them as a fault.
    """
    rc = entry.run(
        make_settings(tmp_path, bucket=None),
        wait_for_shutdown=lambda c: entry._LIFETIME_REASON,
    )
    assert rc == 0


def test_a_crashed_backend_is_a_failure(wired, tmp_path):  # noqa: F811
    """The case the platform has to be able to see."""
    rc = entry.run(
        make_settings(tmp_path, bucket=None),
        wait_for_shutdown=lambda c: "backend exited (code 1)",
    )
    assert rc != 0, "a dead backend reported success to ECS"


def test_a_crashed_front_is_a_failure(wired, tmp_path):  # noqa: F811
    rc = entry.run(
        make_settings(tmp_path, bucket=None),
        wait_for_shutdown=lambda c: "front exited (code 137)",
    )
    assert rc != 0


def test_an_unaccountable_reason_is_a_failure(wired, tmp_path):  # noqa: F811
    """An empty reason is not evidence that things went well.

    Defaulting the unknown case to success is what made the original bug quiet, so
    the unknown case fails closed instead.
    """
    rc = entry.run(make_settings(tmp_path, bucket=None), wait_for_shutdown=lambda c: "")
    assert rc != 0


def test_teardown_still_runs_on_the_failure_path(wired, tmp_path):  # noqa: F811
    """Reporting a failure must not skip the cleanup, which has to be unconditional.

    ``_teardown`` is in a ``finally``, so a non-zero return must not become a way to leave
    a child undrained. ``wired`` records the call order, so the assertion is against what
    actually ran.
    """
    entry.run(
        make_settings(tmp_path, bucket=None),
        wait_for_shutdown=lambda c: "backend exited (code 1)",
    )
    # ``wired`` records a ``term:<name>`` per child stopped. Asserted against those
    # real event names rather than a name I assumed: the first version of this test
    # looked for "teardown" and failed against a correct implementation.
    assert [e for e in wired if e.startswith("term:")] == ["term:front", "term:backend"], wired


# --- the final backup cycle is the second half of an orderly stop -----------------


class _FakeSidecar:
    """A sidecar whose drain reports the status this test wants to exercise."""

    pid = 8321

    def __init__(self, status: int | None) -> None:
        self._status = status

    def terminate(self, drain_timeout: float) -> int | None:
        return self._status


def _stop_with_sidecar(monkeypatch, tmp_path, status: int | None) -> int:
    """Take an orderly stop with a sidecar that drained reporting *status*."""
    monkeypatch.setattr(entry, "restore_authority", lambda settings: None)
    monkeypatch.setattr(entry, "_start_sidecar", lambda settings: _FakeSidecar(status))
    monkeypatch.setattr(entry, "_sweep_orphans_the_backend_cannot_reap", lambda known: None)
    return entry.run(make_settings(tmp_path, bucket="bkt"), wait_for_shutdown=lambda c: "signal")


@pytest.mark.usefixtures("wired")
def test_a_committed_final_cycle_keeps_an_orderly_stop_successful(tmp_path, monkeypatch):
    """The success case has to stay reachable, or the check is just a broken task."""
    assert _stop_with_sidecar(monkeypatch, tmp_path, 0) == 0


@pytest.mark.usefixtures("wired")
def test_a_failed_final_cycle_makes_an_orderly_stop_a_failure(tmp_path, monkeypatch):
    """The writer exits non-zero for a post-shutdown cycle it could not complete.

    That cycle runs after the backend's flush and holds the only copy of the turns in
    it, so exiting 0 here reports a lossless replacement for a lossy one.
    """
    assert _stop_with_sidecar(monkeypatch, tmp_path, 1) != 0


@pytest.mark.usefixtures("wired")
def test_a_sidecar_killed_at_the_drain_window_is_a_failure(tmp_path, monkeypatch):
    """A drain overrun mid-upload is reported as a negative status, not a code."""
    assert _stop_with_sidecar(monkeypatch, tmp_path, -9) != 0


@pytest.mark.usefixtures("wired")
def test_an_unreadable_sidecar_status_is_a_failure(tmp_path, monkeypatch):
    """Unknown fails closed here for the same reason an empty shutdown reason does."""
    assert _stop_with_sidecar(monkeypatch, tmp_path, None) != 0
