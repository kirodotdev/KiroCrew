"""``unblocked_signals`` and ``process_group_report`` in ``spawn_test_helpers``.

A test that interrupts a child it starts needs that child to take the signal:
an ignore inherited from the worker must not reach it, and a child that still
outlives the signal must leave a report of why.
"""

from __future__ import annotations

import signal
import subprocess
import sys

import pytest
from spawn_test_helpers import plain_sleeper_argv, process_group_report, unblocked_signals

from kiro_crew import platform_compat
from kiro_crew.testing.wait import default_timeout

#: The interrupts a test hands a child: SIGHUP is the one a run under ``nohup`` ignores.
_SIGNAL_NAMES = ("SIGINT", "SIGTERM", "SIGHUP")

# Python puts its own SIGINT handler over a default (never over an ignore), so
# "taken" is anything but SIG_IGN.
_REPORT_DISPOSITIONS = (
    "import signal; "
    f"print(*(signal.getsignal(getattr(signal, n)) != signal.SIG_IGN for n in {_SIGNAL_NAMES!r}))"
)


@pytest.mark.skipif(not platform_compat.IS_POSIX, reason="POSIX signal dispositions")
def test_an_inherited_ignore_does_not_reach_the_child_and_comes_back():
    before = {getattr(signal, n): signal.getsignal(getattr(signal, n)) for n in _SIGNAL_NAMES}
    try:
        for number in before:
            signal.signal(number, signal.SIG_IGN)
        with unblocked_signals():
            out = subprocess.run(
                [sys.executable, "-c", _REPORT_DISPOSITIONS],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=default_timeout(),
                check=True,
            ).stdout
        after = {s: signal.getsignal(s) for s in before}
    finally:
        for number, handler in before.items():
            signal.signal(number, handler)
    assert out.split() == ["True"] * len(_SIGNAL_NAMES), out
    assert after == dict.fromkeys(before, signal.SIG_IGN)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
def test_the_group_report_names_each_member_and_its_signals():
    proc = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"],
        stdin=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        report = process_group_report(proc.pid)
    finally:
        proc.stdin.close()
        proc.wait(timeout=default_timeout())
    assert f"pid {proc.pid}:" in report, report
    for key in ("State:", "SigBlk:", "SigIgn:", "SigPnd:", "wchan:"):
        assert key in report, report
    assert "no live member" in process_group_report(proc.pid)


@pytest.mark.skipif(not platform_compat.IS_POSIX, reason="POSIX sleep and shebang scripts")
@pytest.mark.parametrize(
    ("head", "expect_script"),
    [(b"#!/usr/bin/coreutils --coreutils-prog-shebang=sleep\n", True), (b"\x7fELF\x02", False)],
    ids=["coreutils-single-script", "plain-binary"],
)
def test_the_sleeper_steps_around_a_sleep_that_is_a_script(
    tmp_path, monkeypatch, head, expect_script
):
    """A ``sleep`` script would show its interpreter's argv, so the helper avoids it."""
    fake = tmp_path / "sleep"
    fake.write_bytes(head)
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))

    argv = plain_sleeper_argv(7)

    if expect_script:
        assert argv == [sys.executable, "-c", "import time; time.sleep(7)"]
    else:
        assert argv == [str(fake), "7"]
