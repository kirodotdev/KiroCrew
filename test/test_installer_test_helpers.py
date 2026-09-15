"""Pins for ``run_bounded``'s timeout kill path.

The PowerShell installer tests skip only when no ``pwsh``/``powershell``
binary exists, so they DO run on Windows CI and reach ``run_bounded`` — a
platform where ``os.killpg`` does not exist. These pins hold the two
guarantees those callers depend on: ``subprocess.TimeoutExpired`` (never a
platform ``AttributeError``) propagates from the timeout path on every
platform shape, so ``except (TimeoutExpired, OSError): pytest.skip(...)``
guards fire; and the kill reaps the whole tree — observed via an elapsed
bound, because an unreaped child holding the pipes rides out the post-kill
drain budget instead of returning promptly.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest
from installer_test_helpers import _POST_KILL_DRAIN_TIMEOUT, run_bounded

from kiro_crew import platform_compat

#: Long enough that only the kill path (never the child exiting on its own)
#: can end the run before the assertion; well above the elapsed bound below
#: and the helper's post-kill drain budget, so a leak is distinguishable.
_CHILD_SLEEP_SECS = 30.0

#: Overhead slack on top of ``run_bounded``'s own timeout. A real kill returns
#: in well under a second; a leaked descendant makes the helper ride out its
#: full drain budget, so any bound strictly below ``timeout + drain`` separates
#: the two. Defined against the drain constant so the separation cannot drift
#: if the budget changes.
_REAPED_WITHIN_SECS = _POST_KILL_DRAIN_TIMEOUT - 1.0

_SLEEPER = [sys.executable, "-c", f"import time; time.sleep({_CHILD_SLEEP_SECS})"]

# Spawns a grandchild that inherits the stdout/stderr pipes, then wedges. Only
# a tree kill closes the grandchild's pipe handles; killing the direct child
# alone leaves the drain blocked until its budget expires.
_SLEEPER_WITH_GRANDCHILD = [
    sys.executable,
    "-c",
    (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', 'import time; time.sleep({_CHILD_SLEEP_SECS})']); "
        f"time.sleep({_CHILD_SLEEP_SECS})"
    ),
]


def _run_expecting_timeout(argv: list[str], timeout: float) -> float:
    """Run ``run_bounded`` expecting a timeout; return the elapsed seconds."""
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_bounded(argv, env=dict(os.environ), timeout=timeout)
    return time.monotonic() - started


def test_timeout_on_a_windows_shaped_platform_reraises_and_reaps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Windows branch re-raises TimeoutExpired — never AttributeError.

    Forcing ``IS_POSIX = False`` routes the kill through the Windows ladder
    (job termination is inert here, and on a POSIX host with no ``taskkill``
    the ``kill_process_tree`` call raises ``OSError`` into the direct-kill
    fallback) — the platform shape where an unconditional ``os.killpg`` call
    raises ``AttributeError``.
    """
    monkeypatch.setattr(platform_compat, "IS_POSIX", False)
    elapsed = _run_expecting_timeout(_SLEEPER, timeout=0.2)
    assert elapsed < 0.2 + _REAPED_WITHIN_SECS, f"child not reaped promptly ({elapsed:.1f}s)"


def test_timeout_reaps_the_whole_process_tree() -> None:
    """A wedged grandchild holding the pipes is reaped with the child.

    If only the direct child died, the grandchild's inherited pipe handles
    would hold ``communicate()`` open for the full post-kill drain budget and
    this elapsed bound would trip. The 1s timeout (vs 0.2s above) gives the
    child time to actually spawn the grandchild on a loaded runner — a kill
    landing on a still-childless child would make this pin vacuous.
    """
    elapsed = _run_expecting_timeout(_SLEEPER_WITH_GRANDCHILD, timeout=1.0)
    assert elapsed < 1.0 + _REAPED_WITHIN_SECS, f"tree not reaped promptly ({elapsed:.1f}s)"
