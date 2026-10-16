"""The autouse fixture and stall doubles the bounded path-resolution tests share.

``test_security_path_resolve_bounded.py`` and its ``_child`` sibling import these by name:
the fixture that resets the resolver's state and scales its budgets down for every test,
the shipped budgets and the real classifier of the sampled ``/proc`` syscall, captured
before it stubs them, and a resolver that stalls the way a wedged child does.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator

import pytest

import kiro_crew.executors as ex
from kiro_crew import security
from kiro_crew.subprocess_pool import SubprocessPoolTimeout

# Captured BEFORE the autouse fixture below can stub it.  The fixture replaces this
# helper for every test in the files that import it, so a test that wants to exercise
# the real state parsing has to hold its own reference or it silently asserts against
# the stub.
_REAL_BLOCKED_IN_FILESYSTEM = security.paths._child_blocked_in_filesystem

# Likewise captured before the fixture scales them down: the tests that assert on the
# SHIPPED budgets have to read the shipped values, not the fast ones the stall tests run
# under, or they would assert the fixture's numbers back at themselves.
_REAL_CANDIDATE_BUDGET = security.paths._PATH_RESOLVE_TIMEOUT_SECS
_REAL_REBUILD_BUDGET = security.paths._PATH_RESOLVE_REBUILD_TIMEOUT_SECS
_REAL_GRACE_MAX = security.paths._PATH_RESOLVE_GRACE_MAX_SECS
_REAL_WAIT_CAP = security.paths._PATH_RESOLVE_WAIT_CAP_SECS


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing module autouse fixture, moved verbatim; the two tests that call monkeypatch.undo() do so to drop exactly these stubs and read the shipped values
def _fresh_resolver_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(security, "_path_resolve_degraded", {})
    # Short budgets keep the stall tests fast; the production values are pinned
    # separately, in ``test_security_path_resolve_bounded.py``.
    monkeypatch.setattr(security, "_PATH_RESOLVE_TIMEOUT_SECS", 0.2)
    # The anchor rebuild carries its OWN, larger budget in production. Scaled down
    # here for the same reason as the one above: a stall test that reaches the
    # rebuild would otherwise wait the full production budget.
    monkeypatch.setattr(security.paths, "_PATH_RESOLVE_REBUILD_TIMEOUT_SECS", 0.2)
    monkeypatch.setattr(security, "_PATH_RESOLVE_COOLDOWN_SECS", 30.0)
    # The stall doubles below stand in for a WEDGED MOUNT, so they must stand in for its
    # kernel state too: a real ``lstat`` on a dead mount sits in uninterruptible sleep,
    # whereas the stubs carry no real child sample and would read as merely descheduled.
    # Without this, every cooldown assertion in the files that import it would exercise
    # the load arm instead.
    # The tests that DO exercise that arm override this locally.
    # Patched on the OWNING module, not the package alias: ``_run_resolution_bounded``
    # calls this as a module global in ``security.paths``, so rebinding the re-exported
    # name on the package would not be seen by the code under test.
    monkeypatch.setattr(security.paths, "_child_blocked_in_filesystem", lambda sampled: True)
    yield
    # Drop the pool so a child a test left in an odd state cannot leak into the
    # next test's timing.
    ex.shutdown_maintenance_executor()


def _stall_like_a_child() -> None:
    """What a wedged child looks like from the calling thread.

    The pool waits out the shared deadline, kills the child, and raises with the
    syscall it sampled first; the stubs below do the same so the classifier under test
    sees exactly the exception it sees in production.
    """
    deadline = getattr(security.paths._child_budget, "deadline", None)
    if deadline is not None:
        # flake-ok: stands in for the pool waiting out the armed deadline before it kills the child; pre-existing, moved verbatim
        time.sleep(max(0.0, deadline - time.monotonic()))
    raise SubprocessPoolTimeout("stub child did not answer", child_syscall=b"6")


class _StalledResolver:
    """Stands in for the resolver child on a wedged automount: never answers
    within the deadline, raises nothing itself."""

    def __init__(self) -> None:
        self.release = threading.Event()  # kept so callers' ``release.set()`` stay valid
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def __call__(self, expanded: str) -> set[str]:
        with self._lock:
            self.calls.append(expanded)
        _stall_like_a_child()
        return {expanded}
