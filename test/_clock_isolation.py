"""Check that a test's ManualClock reaches only the module it was installed on.

A fake ``sleep`` set as ``<module>.time.sleep`` replaces the stdlib ``time.sleep``
for every thread in the worker, because ``<module>.time`` IS the stdlib module.
Another live thread then returns from its sleep at once, lands in the test's
recorded schedule and moves the test's deadline. :func:`assert_clock_is_private`
runs a real thread while the clock is installed and fails if that thread sees the
fake, so a test helper that slips back onto the shared module fails here, on every
run, instead of flaking under load.
"""

from __future__ import annotations

import threading
import time
import types

from kiro_crew.testing.clock import ManualClock

#: Taken when this module is imported, before any test patches anything, so a
#: helper that rebinds the stdlib ``time.sleep`` cannot also rebind the reference.
_REAL_SLEEP = time.sleep


def assert_clock_is_private(clock: ManualClock, module: types.ModuleType) -> None:
    """Fail unless ``module`` reads ``clock`` and another thread reads real time.

    ``clock`` must already be installed on ``module``. It is advanced far past real
    monotonic time first, so a neighbour reading the clock cannot pass by accident.
    """
    clock.advance(10_000_000.0)
    seen: dict[str, object] = {}

    def _neighbour() -> None:
        seen["sleep"] = time.sleep
        seen["monotonic"] = time.monotonic()

    thread = threading.Thread(target=_neighbour, daemon=True)
    thread.start()
    thread.join(30.0)
    assert not thread.is_alive()
    assert seen["sleep"] is _REAL_SLEEP, "another thread's sleep would reach the test's clock"
    assert seen["monotonic"] != clock.monotonic(), "another thread read the test's clock"
    assert module.time.monotonic() == clock.monotonic()
