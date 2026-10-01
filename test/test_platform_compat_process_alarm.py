"""``platform_compat.arm_process_alarm``: the loop watchdog's GIL-free last resort.

Both branches are pinned through stubbed ``signal`` attributes, so the tests run
the same on every CI runner.
"""

from __future__ import annotations

import signal
from pathlib import Path

import pytest

from kiro_crew import platform_compat as pc

# ── process alarm (the watchdog's GIL-free last resort) ──────────────────────


def test_process_alarm_arms_and_cancels_through_setitimer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, float]] = []
    monkeypatch.setattr(
        signal, "setitimer", lambda which, secs: calls.append((which, secs)), raising=False
    )
    monkeypatch.setattr(signal, "ITIMER_REAL", 99, raising=False)
    assert pc.process_alarm_available() is True
    assert pc.arm_process_alarm(100.0) is True
    assert pc.arm_process_alarm(0.0) is True
    assert calls == [(99, 100.0), (99, 0.0)]


def test_process_alarm_reports_unavailable_without_setitimer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(signal, "setitimer", raising=False)
    assert pc.process_alarm_available() is False
    assert pc.arm_process_alarm(100.0) is False


def _fake_itimer(monkeypatch: pytest.MonkeyPatch) -> dict[str, float]:
    """A stand-in ``ITIMER_REAL`` so the tests never touch the real one, which a
    test worker's own timeout may own."""
    timer = {"value": 0.0}
    monkeypatch.setattr(
        signal, "setitimer", lambda which, secs: timer.__setitem__("value", secs), raising=False
    )
    monkeypatch.setattr(signal, "getitimer", lambda which: (timer["value"], 0.0), raising=False)
    monkeypatch.setattr(signal, "ITIMER_REAL", 99, raising=False)
    return timer


@pytest.mark.parametrize("seam", ["reexec_launcher", "reexec_python_module"])
def test_reexec_cancels_the_process_alarm_before_the_exec(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, seam: str
) -> None:
    """``execve`` preserves ``ITIMER_REAL`` and resets a caught ``SIGALRM`` to
    its default disposition, so a pending loop-stall deadline would end the
    successor during its own boot, with no dump.  Both exec seams cancel it with
    nothing between the cancel and the exec: the timer reads zero when
    ``os.execv`` is reached."""
    _fake_itimer(monkeypatch)
    seen: list[tuple[float, float]] = []
    monkeypatch.setattr(
        pc.os, "execv", lambda path, argv: seen.append(signal.getitimer(signal.ITIMER_REAL))
    )
    assert pc.arm_process_alarm(25.0) is True  # the last heartbeat's deadline
    assert signal.getitimer(signal.ITIMER_REAL) == (25.0, 0.0)
    if seam == "reexec_launcher":
        pc.reexec_launcher(str(tmp_path / "launcher"), ["gateway"])
    else:
        pc.reexec_python_module("kiro_crew", ["gateway"], executable=str(tmp_path / "python"))
    assert seen == [(0.0, 0.0)]
