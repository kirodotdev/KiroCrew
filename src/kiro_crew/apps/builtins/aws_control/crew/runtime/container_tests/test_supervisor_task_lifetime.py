"""The task's own deadline, which is the only bound left when nothing outside acts.

A Fargate task is unattended. The launcher sweeps its own leftovers when it
launches again, and that sweep is the enforcement an operator sees in ordinary
use -- but it cannot reach a cluster whose last launch has already happened. A
task that carries its deadline stops billing with no further launch, no
scheduler, and the owner's gateway switched off.

The wait loop is driven directly here rather than through ``run``: the point of
these cases is which reason the loop returns and when, and ``run``'s own suite
already covers what each reason does to the exit code.
"""

from __future__ import annotations

import dataclasses
import os
import signal
from unittest import mock

import pytest
from container.supervisor import __main__ as entry

from .test_supervisor_main import make_settings, wired  # noqa: F401  (pytest fixture)


@pytest.fixture(autouse=True)
def _restore_signal_handlers():
    """Put back what the wait loop installs.

    ``_wait_for_shutdown`` registers SIGTERM and SIGINT handlers on the process it
    runs in. Driving it directly means this test process gets them, and leaving
    them in place would change how the rest of the run answers an interrupt.
    """
    saved = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        yield
    finally:
        for sig, handler in saved.items():
            signal.signal(sig, handler)


class Child:
    """A watched process that stays alive, or reports an exit when told to.

    ``alive_polls`` makes it outlive a number of polls and then exit. Every case
    here then ENDS on its own: if the deadline branch stops working, the wait
    returns this child's exit instead of blocking until the suite's timeout, so a
    broken bound fails the assertion rather than hanging the run.
    """

    def __init__(
        self, name: str = "backend", code: int | None = None, alive_polls: int = 0
    ) -> None:
        self.name = name
        self._code = code
        self._alive_polls = alive_polls
        self.pid = os.getpid()

    def poll(self):
        if self._alive_polls > 0:
            self._alive_polls -= 1
            return None
        return self._code

    def returncode(self):
        return self._code


def test_a_spent_lifetime_ends_the_wait():
    """The deadline fires on its own, with no signal and no child exiting.

    The child is alive across the deadline and exits after it, so a wait with no
    working deadline returns the exit instead and this fails in a few seconds.
    """
    why = entry._wait_for_shutdown([Child(code=1, alive_polls=8)], ttl_seconds=1)
    assert why == entry._LIFETIME_REASON


def test_a_lifetime_stop_is_an_orderly_reason():
    """The reason has to be one ``run`` reports as success, or the bound reads as a fault."""
    assert entry._LIFETIME_REASON in entry._ORDERLY_REASONS
    assert "signal" in entry._ORDERLY_REASONS


def test_a_child_that_exits_first_still_wins():
    """A crash during a bounded task is still a crash.

    Non-vacuity for the deadline: a loop that returned the lifetime reason for
    every ending would hide the case the exit code exists to show.
    """
    why = entry._wait_for_shutdown([Child(name="backend", code=1)], ttl_seconds=60)
    assert why == "backend exited (code 1)"


def test_an_unbounded_wait_ignores_the_deadline_branch():
    """Zero seconds means no deadline, which is the behaviour without this setting.

    Driven by a child that outlives what would have been a deadline: reaching its
    exit is what proves nothing fired first. A deadline armed at zero would return
    the lifetime reason here instead.
    """
    why = entry._wait_for_shutdown([Child(name="front", code=137, alive_polls=4)], ttl_seconds=0)
    assert why == "front exited (code 137)"


def test_a_bound_beyond_float_range_is_a_long_life_not_a_crash():
    """An integer TTL too large for a float must not end the task by raising.

    ``cloud.json`` accepts any positive integer for the lifetime, so a hand-written
    one can exceed what a float holds. Adding such a value to the clock raises
    ``OverflowError``, which would take the supervisor down on every launch of that
    lane; comparing elapsed time against it instead simply never reaches it. The
    child exits after a few polls, so the wait ends on that and a bound that fired
    or raised fails here.
    """
    why = entry._wait_for_shutdown(
        [Child(name="front", code=3, alive_polls=4)], ttl_seconds=10**400
    )
    assert why == "front exited (code 3)"


def test_run_hands_the_configured_lifetime_to_the_wait(wired, tmp_path):  # noqa: F811
    """The setting has to REACH the loop, which no injected stub can show.

    Every other supervise-phase test replaces the wait outright, so the binding
    between the container's configuration and the loop's deadline is invisible to
    them: removing it would leave each of them passing while no task is bounded.
    """
    seen: dict[str, int] = {}

    def recorder(children, *, ttl_seconds=0):
        seen["ttl_seconds"] = ttl_seconds
        return "signal"

    settings = dataclasses.replace(make_settings(tmp_path, bucket=None), task_ttl_seconds=4242)
    with mock.patch.object(entry, "_wait_for_shutdown", recorder):
        assert entry.run(settings) == 0
    assert seen == {"ttl_seconds": 4242}
