"""The supervisor's exit code is the only signal the platform reads.

``run()`` ended with an unconditional ``return 0`` after teardown, so a task whose
backend had crashed reported the same status as one ECS had asked to stop. On the
console that is a task exiting normally, over and over, with nothing marked failed --
the crash loop is invisible exactly when it matters.

Nothing asserted the return value before, which is how it survived: the whole
supervisor suite passed with the bug in place.
"""

from __future__ import annotations

from container.supervisor import __main__ as entry

from .test_supervisor_main import make_settings, wired  # noqa: F401  (pytest fixture)


def test_an_orderly_signal_is_success(wired, tmp_path):  # noqa: F811
    rc = entry.run(make_settings(tmp_path, bucket=None), wait_for_shutdown=lambda c: "signal")
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
    """Reporting a failure must not skip the cleanup that used to be unconditional.

    ``_teardown`` is in a ``finally`` and the final backup cycle lives inside it, so
    a non-zero return must not become a way to lose the last turn. ``wired`` records
    the call order, so the assertion is against what actually ran.
    """
    entry.run(
        make_settings(tmp_path, bucket=None),
        wait_for_shutdown=lambda c: "backend exited (code 1)",
    )
    # ``wired`` records a ``term:<name>`` per child stopped. Asserted against those
    # real event names rather than a name I assumed: the first version of this test
    # looked for "teardown" and failed against a correct implementation.
    assert [e for e in wired if e.startswith("term:")] == ["term:front", "term:backend"], wired
