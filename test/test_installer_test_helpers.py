"""Pins for ``run_bounded``'s timeout kill path.

The PowerShell installer tests skip only when no ``pwsh``/``powershell``
binary exists, so they DO run on Windows CI and reach ``run_bounded`` — a
platform where ``os.killpg`` does not exist. These pins hold the two
guarantees those callers depend on: ``subprocess.TimeoutExpired`` (never a
platform ``AttributeError``) propagates from the timeout path on every
platform shape, so ``except (TimeoutExpired, OSError): pytest.skip(...)``
guards fire; and the kill reaps the whole tree — observed via the post-kill
drain, which reads every pipe to EOF only once no process of the tree still
holds them, and otherwise rides out its budget to ``TimeoutExpired``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from installer_test_helpers import _POST_KILL_DRAIN_TIMEOUT, run_bounded

from kiro_crew import platform_compat
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, wait_until

#: Long enough that only the kill path (never the child exiting on its own)
#: can end the run, and well above the helper's post-kill drain budget, so a
#: leaked descendant still holds the pipes when the drain gives up.
_CHILD_SLEEP_SECS = 30.0

_SLEEPER = [sys.executable, "-c", f"import time; time.sleep({_CHILD_SLEEP_SECS})"]

#: Names the file the grandchild creates once it runs holding the inherited pipes.
_GRANDCHILD_READY_ENV = "KIRO_CREW_TEST_GRANDCHILD_READY"

# Spawns a grandchild that inherits the stdout/stderr pipes, then wedges. The
# grandchild creates the file named by ``_GRANDCHILD_READY_ENV`` once it runs,
# so the test knows a second pipe holder exists. Only a tree kill closes the
# grandchild's pipe handles; killing the direct child alone leaves the drain
# blocked until its budget expires.
_SLEEPER_WITH_GRANDCHILD = [
    sys.executable,
    "-c",
    (
        "import subprocess, sys, time; "
        "subprocess.Popen([sys.executable, '-c', "
        "'import os, pathlib, time; "
        f'pathlib.Path(os.environ["{_GRANDCHILD_READY_ENV}"]).touch(); '
        f"time.sleep({_CHILD_SLEEP_SECS})']); "
        f"time.sleep({_CHILD_SLEEP_SECS})"
    ),
]


def _drain_outcomes(
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    timeout: float,
    ready: Path | None = None,
) -> list[str]:
    """Run ``run_bounded`` expecting a timeout; return how each post-kill drain ended.

    Records every ``communicate`` call handed the drain budget as ``"eof"`` (all
    pipe holders gone) or ``"timed out"`` (a process of the tree still holds them).
    With *ready*, the timed wait under test starts only once that file exists, so
    the kill lands on a tree that already has its grandchild. Only the child
    ``run_bounded`` spawns for *argv* is watched: any other subprocess (the
    ``taskkill`` the Windows tree kill runs, say) passes through unrecorded.
    """
    outcomes: list[str] = []
    real = subprocess.Popen
    target: list[subprocess.Popen] = []

    class _RecordingPopen(real):  # type: ignore[misc, valid-type]
        def __init__(self, args, *a, **kw):
            super().__init__(args, *a, **kw)
            if not target and args == argv:
                target.append(self)

        def communicate(self, input=None, timeout=None):
            if not target or self is not target[0]:
                return super().communicate(input, timeout)
            if timeout != _POST_KILL_DRAIN_TIMEOUT:
                if ready is not None:
                    try:
                        wait_until(ready.exists, timeout=FALLBACK_TIMEOUT_SECS)
                    except AssertionError:
                        self.kill()  # lost run: do not leave Popen's exit waiting on it
                        raise AssertionError("the grandchild never started") from None
                return super().communicate(input, timeout)
            try:
                result = super().communicate(input, timeout)
            except subprocess.TimeoutExpired:
                outcomes.append("timed out")
                raise
            outcomes.append("eof")
            return result

    monkeypatch.setattr(subprocess, "Popen", _RecordingPopen)
    with pytest.raises(subprocess.TimeoutExpired):
        run_bounded(argv, env=dict(os.environ), timeout=timeout)
    return outcomes


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
    outcomes = _drain_outcomes(monkeypatch, _SLEEPER, timeout=0.2)
    assert outcomes == ["eof"], f"child not reaped: the post-kill drain {outcomes}"


def test_timeout_reaps_the_whole_process_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A wedged grandchild holding the pipes is reaped with the child.

    If only the direct child died, the grandchild's inherited pipe handles
    would hold ``communicate()`` open for the full post-kill drain budget and
    the drain would end in ``TimeoutExpired``. The timed wait starts only after
    the grandchild reports it runs, so the kill always lands on a tree with a
    second pipe holder: a kill on a still-childless child cannot pass vacuously.
    """
    ready = tmp_path / "grandchild-ready"
    monkeypatch.setenv(_GRANDCHILD_READY_ENV, str(ready))
    outcomes = _drain_outcomes(monkeypatch, _SLEEPER_WITH_GRANDCHILD, timeout=0.2, ready=ready)
    assert outcomes == ["eof"], f"tree not reaped: the post-kill drain {outcomes}"
