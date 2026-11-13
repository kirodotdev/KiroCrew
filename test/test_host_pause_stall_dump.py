"""A death during a host-wide pause is told apart from a loop stall at the next boot.

The watchdog keeps the armed stall alarm's deadline in the dump header; the kernel
delivers an expired alarm only once the process runs again, so a dump whose stacks
were written well after that deadline belongs to a process the host stopped
running. The next boot's readers (cautious boot, the stall attribution the doctor
and the boot notification print, the cron breaker) then blame the host, not the
gateway. A dump's name is when its gateway started, so readers also show when it
fired.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from kiro_crew import resource_status
from kiro_crew.dashboard import crash_dump_store
from kiro_crew.dashboard.cautious_boot import _evaluate
from kiro_crew.dashboard.crash_dump_store import _is_header_only, open_dump_file
from kiro_crew.dashboard.loop_watchdog import LoopStallWatchdog
from kiro_crew.stall_attribution import attribute_dump, describe

pytestmark = pytest.mark.skipif(not hasattr(os, "pwrite"), reason="the header field needs pwrite")

#: An arbitrary wall-clock instant the tests place the alarm's deadline at.
_DUE = 1_762_540_000

_STACKS = (
    "Thread 0x00007f0000000001 (most recent call first):\n"
    '  File "/x/kiro_crew/history_search.py", line 10 in _list_sessions_status\n'
    "\n"
    "Current thread 0x00007f0000000002 (most recent call first):\n"
    '  File "/x/kiro_crew/cron.py", line 20 in _run_job_isolated\n'
)


@pytest.fixture(autouse=True)
def _no_active_dump(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """open_dump_file records the process-wide active dump; keep it test-local."""
    _floor_monkeypatch.setattr(crash_dump_store, "_active_dump_file", None)


def _fired(dump_path: Path, *, at: float) -> Path:
    """Append the alarm's stacks to *dump_path* and date the write *at*."""
    with dump_path.open("a", encoding="utf-8") as f:
        f.write(_STACKS)
    os.utime(dump_path, (at, at))
    return dump_path


def test_the_header_records_the_alarm_deadline_in_place(tmp_path: Path) -> None:
    dump = open_dump_file(tmp_path)
    before = dump.path.read_bytes()
    assert b"alarm-due=----------\n" in before

    crash_dump_store.record_alarm_due(dump, _DUE)
    after = dump.path.read_bytes()
    assert f"alarm-due={_DUE}\n".encode() in after
    # Same length and line count: a header-only dump is still header-only.
    assert len(after) == len(before)
    assert _is_header_only(dump.path)

    crash_dump_store.record_alarm_due(dump, None)
    assert dump.path.read_bytes() == before


def test_an_alarm_that_fired_long_after_its_deadline_is_a_host_pause(tmp_path: Path) -> None:
    dump = open_dump_file(tmp_path)
    crash_dump_store.record_alarm_due(dump, _DUE)
    _fired(dump.path, at=_DUE + 65)

    assert crash_dump_store.dump_host_pause_secs(dump.path) == pytest.approx(65.0)


#: Just under the store's 20 s floor for "late".
_ON_TIME_MAX = 19.0


@pytest.mark.parametrize("late", [0.0, 3.0, _ON_TIME_MAX])
def test_an_alarm_that_fired_on_time_is_a_stall(tmp_path: Path, late: float) -> None:
    dump = open_dump_file(tmp_path)
    crash_dump_store.record_alarm_due(dump, _DUE)
    _fired(dump.path, at=_DUE + late)

    assert crash_dump_store.dump_host_pause_secs(dump.path) is None


def test_no_recorded_deadline_or_no_stacks_is_never_a_host_pause(tmp_path: Path) -> None:
    unset = _fired(open_dump_file(tmp_path / "a").path, at=_DUE + 600)
    assert crash_dump_store.dump_host_pause_secs(unset) is None

    header_only = open_dump_file(tmp_path / "b")
    crash_dump_store.record_alarm_due(header_only, _DUE)
    os.utime(header_only.path, (_DUE + 600, _DUE + 600))
    assert crash_dump_store.dump_host_pause_secs(header_only.path) is None


def test_the_fire_time_is_the_last_write_not_the_name(tmp_path: Path) -> None:
    dump = open_dump_file(tmp_path)
    _fired(dump.path, at=_DUE)

    assert crash_dump_store.dump_fired_at(dump.path) == "2025-11-07T18:26:40Z"


class _Clocks:
    """The watchdog's monotonic and wall clocks, moved together by the test."""

    def __init__(self) -> None:
        self.mono = 1000.0
        self.wall = float(_DUE - 25)

    def advance(self, secs: float) -> None:
        self.mono += secs
        self.wall += secs


def _watchdog(dump: crash_dump_store.DumpFile, clocks: _Clocks) -> LoopStallWatchdog:
    return LoopStallWatchdog(
        stall_after=30.0,
        exit_after=25.0,
        # Never polls on its own: the test calls check().
        poll_interval=3600.0,
        now=lambda: clocks.mono,
        wall=lambda: clocks.wall,
        suspend_clock=lambda: None,
        dump=lambda: None,
        arm_later=lambda _t: None,
        cancel_later=lambda: None,
        dump_file=dump,
        enrich=lambda _s: [],
        loop_major_faults=lambda _tid: None,
        log=logging.getLogger("test.host_pause"),
    )


def test_the_watchdog_keeps_the_deadline_current_and_clears_it_when_off(tmp_path: Path) -> None:
    dump = open_dump_file(tmp_path)
    clocks = _Clocks()
    wd = _watchdog(dump, clocks)
    wd.start()
    try:
        wd.check()
        assert f"alarm-due={_DUE}\n" in dump.path.read_text(encoding="utf-8")

        clocks.advance(5.0)
        wd.beat()
        wd.check()
        assert f"alarm-due={_DUE + 5}\n" in dump.path.read_text(encoding="utf-8")
    finally:
        wd.stop()
    # The alarm is off: a soft dump written into the file from here on must not
    # read as an alarm that fired late.
    wd.check()
    assert "alarm-due=----------\n" in dump.path.read_text(encoding="utf-8")


def test_a_host_wide_pause_is_not_blamed_on_the_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole host stops for 90 s against a 25 s budget, and four gateways die on resume."""
    dumps_dir = tmp_path / "crash-dumps"
    dump = open_dump_file(dumps_dir)
    clocks = _Clocks()
    wd = _watchdog(dump, clocks)
    wd.start()
    try:
        wd.check()  # the last poll before the host stopped
    finally:
        wd.stop()
    # The alarm was due 25 s after the last beat and is delivered on resume,
    # 90 s after it.
    _fired(dump.path, at=clocks.wall + 90)

    attribution = attribute_dump(dump.path, tmp_path / "cron")
    assert attribution.host_pause_secs == pytest.approx(65.0)
    lines = describe(attribution)
    assert "host-wide pause" in " ".join(lines)
    assert not any(line.startswith("recommended: kirocrew cron pause") for line in lines)

    class _Dash:
        cautious_boot = True

    class _Cfg:
        dashboard = _Dash()

    class _Status:
        posture = resource_status.POSTURE_AMPLE

    monkeypatch.setattr(resource_status, "probe", lambda cfg=None: _Status())
    # A minute old, so recent enough for a stagger.
    monkeypatch.setattr("kiro_crew.dashboard.cautious_boot.dump_age_seconds", lambda _p: 60.0)
    decision = _evaluate(cfg=_Cfg(), dumps_dir=dumps_dir)
    assert decision.active is False
    assert "host stopped running" in decision.reason
