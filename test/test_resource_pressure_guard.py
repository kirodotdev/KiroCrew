"""The memory guard's trigger and fast sampling in the resource-pressure notifier.

rfc-host-memory-guard.md §8 tests 5, 7 and 8: critical fires; tight with PSI
``full avg10`` >= 10 fires; PSI alone on ample does not, unless the memory cgroup is
out of view and the topmost visible cgroup stalled 10% of the time since the last
sample, on a tight or ample reading; a missing PSI file fails open;
``resource_critical_gb = 0`` disables every arm; no reading taken at a hand-off or
while its stop still runs is a baseline, so one stall stops one tree; sampling drops
to the heartbeat's 5 s on tight or critical and while the memory cgroup is out of
view, and returns to 30 s on ample.
"""

from __future__ import annotations

import asyncio
import threading
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

import kiro_crew.subagent as subagent
from kiro_crew.notifications import resource_pressure
from kiro_crew.notifications.bus import NotificationBus
from kiro_crew.notifications.resource_pressure import (
    FAST_SAMPLE_INTERVAL_SECS,
    SAMPLE_INTERVAL_SECS,
    ResourcePressureNotifier,
)
from kiro_crew.platform_compat_metrics import linux_memory_psi_full_avg10
from kiro_crew.resource_status import (
    POSTURE_AMPLE,
    POSTURE_CRITICAL,
    POSTURE_TIGHT,
    POSTURE_UNKNOWN,
    ResourceStatus,
)

#: One reading: the probe's status, the host PSI, and the out-of-view cgroup's total
#: ``full`` stall in microseconds (an exception there is raised by the read).
Reading = tuple[ResourceStatus, float | None, int | BaseException | None]


def _status(posture: str, gb: float = 1.5, critical_gb: float = 2.0) -> ResourceStatus:
    return ResourceStatus(
        available_gb=gb,
        cpu_count=8,
        load_per_cpu=0.5,
        posture=posture,
        pressure_gb=4.0,
        critical_gb=critical_gb,
    )


def _ample() -> ResourceStatus:
    # A container's view of a roomy host: the free figure is the host's.
    return _status(POSTURE_AMPLE, gb=11.3)


def _us(seconds: float) -> int:
    return round(seconds * 1_000_000)


@pytest.fixture(autouse=True)
def _no_slice_io(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    _floor_monkeypatch.setattr(resource_pressure, "check_agents_slice_pressure", lambda: None)
    _floor_monkeypatch.setattr(resource_pressure, "reclaim_agents_slice_page_cache", lambda: None)


def _run(
    readings: list[Reading], clock: list[float]
) -> tuple[ResourcePressureNotifier, list[tuple[str, float | None]], list[float], list[float]]:
    """Sample each reading at the clock values the caller sets.

    Returns the notifier, the guard calls, the clock at each probe, and the clock at
    each read of the out-of-view cgroup's stall.
    """
    calls: list[tuple[str, float | None]] = []
    probed: list[float] = []
    viewed: list[float] = []
    queue = list(readings)
    current: list[Reading] = []

    def probe() -> ResourceStatus:
        current[:] = [queue.pop(0)]
        probed.append(clock[0])
        return current[0][0]

    def view_stall() -> int | None:
        viewed.append(clock[0])
        value = current[0][2]
        if isinstance(value, BaseException):
            raise value
        return value

    notifier = ResourcePressureNotifier(
        NotificationBus(sink=lambda note: None),
        probe_fn=probe,
        clock=lambda: clock[0],
        disk_fn=lambda: (POSTURE_AMPLE, "", None),
        psi_fn=lambda: current[0][1],
        view_stall_fn=view_stall,
    )
    notifier.memory_guard = lambda status, psi, trigger: calls.append((trigger, psi))
    return notifier, calls, probed, viewed


def _sample_at(notifier: ResourcePressureNotifier, clock: list[float], at: list[float]) -> None:
    async def scenario() -> None:
        for when in at:
            clock[0] = when
            await notifier.maybe_sample()

    asyncio.run(scenario())


class TestTrigger:
    @pytest.mark.parametrize(
        ("posture", "psi", "stalled_pct", "expected"),
        [
            (POSTURE_CRITICAL, None, None, ("critical", None)),
            (POSTURE_CRITICAL, 50.0, None, ("critical", 50.0)),
            (POSTURE_TIGHT, 10.0, None, ("psi", 10.0)),
            (POSTURE_TIGHT, 9.99, None, None),
            (POSTURE_TIGHT, None, None, None),  # a missing PSI file fails open
            # Tight while the memory cgroup is out of view: the host PSI is low, but
            # the cgroup in view stalls at a limit the free figure does not show.
            (POSTURE_TIGHT, 2.0, 40.0, ("psi-cgroup", 40.0)),
            (POSTURE_TIGHT, 12.0, 40.0, ("psi", 12.0)),
            (POSTURE_AMPLE, 80.0, None, None),  # PSI alone on an ample host stops nothing
            # Ample while the memory cgroup is out of view: the free figure is the
            # host's, so the stall of the cgroup in view is the evidence, not the host's.
            (POSTURE_AMPLE, 0.0, 10.0, ("psi-cgroup", 10.0)),
            (POSTURE_AMPLE, 0.0, 37.5, ("psi-cgroup", 37.5)),
            (POSTURE_AMPLE, 80.0, 9.99, None),
            (POSTURE_UNKNOWN, 80.0, 80.0, None),  # a failed probe stays a non-signal
        ],
    )
    def test_trigger_per_reading(
        self, posture: str, psi: float | None, stalled_pct: float | None, expected: Any
    ) -> None:
        # A quiet baseline at 0, then the reading under test at 30 s, which stalled
        # *stalled_pct* of the time in between.
        clock = [0.0]
        total = None if stalled_pct is None else _us(stalled_pct / 100 * 30)
        notifier, calls, probed, _ = _run(
            [(_ample(), 0.0, None if total is None else 0), (_status(posture), psi, total)],
            clock,
        )
        _sample_at(notifier, clock, [0.0, 30.0])
        assert probed == [0.0, 30.0]
        assert calls == ([] if expected is None else [expected])

    def test_the_first_stall_reading_is_only_a_baseline(self) -> None:
        clock = [0.0]
        notifier, calls, _, viewed = _run([(_ample(), 0.0, _us(3600))], clock)
        _sample_at(notifier, clock, [0.0])
        assert viewed == [0.0]
        assert calls == []

    @pytest.mark.parametrize("posture", [POSTURE_CRITICAL, POSTURE_TIGHT, POSTURE_AMPLE])
    def test_critical_gb_zero_disables_every_arm(self, posture: str) -> None:
        clock = [0.0]
        notifier, calls, _, viewed = _run(
            [
                (_status(posture, critical_gb=0.0), 90.0, 0),
                (_status(posture, critical_gb=0.0), 90.0, _us(27)),
            ],
            clock,
        )
        _sample_at(notifier, clock, [0.0, 30.0])
        assert calls == []
        assert viewed == []

    @pytest.mark.parametrize(
        ("posture", "read"),
        [
            (POSTURE_TIGHT, True),
            (POSTURE_AMPLE, True),
            (POSTURE_UNKNOWN, True),
            # A critical reading hands off without the stall and drops its baseline.
            (POSTURE_CRITICAL, False),
        ],
    )
    def test_the_stall_is_read_beside_every_armed_probe_but_a_critical_one(
        self, posture: str, read: bool
    ) -> None:
        clock = [0.0]
        notifier, _, _, viewed = _run([(_status(posture), 1.0, 0)], clock)
        _sample_at(notifier, clock, [0.0])
        assert viewed == ([0.0] if read else [])

    def test_a_view_read_that_raises_counts_as_no_reading_and_keeps_the_sample(self) -> None:
        clock = [0.0]
        notifier, calls, probed, _ = _run(
            [
                (_status(POSTURE_TIGHT, gb=3.0), 1.0, None),
                (_ample(), 0.0, RuntimeError("view unreadable")),
                (_status(POSTURE_TIGHT, gb=3.0), 40.0, None),
            ],
            clock,
        )
        published: list[float] = []
        notifier._on_posture_change = lambda: published.append(clock[0])
        _sample_at(notifier, clock, [0.0, FAST_SAMPLE_INTERVAL_SECS, FAST_SAMPLE_INTERVAL_SECS * 7])
        # The ample sample, its view unread, is still published and stops nothing.
        assert probed == [0.0, FAST_SAMPLE_INTERVAL_SECS, FAST_SAMPLE_INTERVAL_SECS * 7]
        assert published == probed
        assert calls == [("psi", 40.0)]

    @pytest.mark.parametrize("missing", [None, RuntimeError("view unreadable")])
    def test_a_missing_stall_reading_drops_the_baseline(self, missing: Any) -> None:
        # Measured from 0, the stall at 60 s would be a 50% share; with the reading at
        # 30 s missing, 60 s is only a new baseline.
        clock = [0.0]
        notifier, calls, probed, _ = _run(
            [(_ample(), 0.0, 0), (_ample(), 0.0, missing), (_ample(), 0.0, _us(30))],
            clock,
        )
        _sample_at(notifier, clock, [0.0, 30.0, 60.0])
        assert probed == [0.0, 30.0, 60.0]
        assert calls == []

    def test_the_default_view_reader_runs_in_the_probe_worker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A container whose memory limit sits on a cgroup above its own view.
        files = {
            "/proc/self/cgroup": "0::/\n",
            "/proc/self/mountinfo": "31 20 0:28 / /sys/fs/cgroup rw - cgroup2 cgroup2 rw\n",
            "/proc/cgroups": "#subsys_name\thierarchy\tnum_cgroups\tenabled\nmemory\t0\t57\t1\n",
            "/sys/fs/cgroup/cgroup.controllers": "cpuset cpu pids\n",
            "/sys/fs/cgroup/cgroup.events": "populated 1\nfrozen 0\n",
        }
        threads: list[int] = []

        def pressure(total: int) -> str:
            return (
                f"some avg10=50.00 avg60=20.00 avg300=5.00 total={total * 2}\n"
                f"full avg10=37.50 avg60=15.00 avg300=4.00 total={total}\n"
            )

        def read(path: object, **_kwargs: object) -> StringIO:
            threads.append(threading.get_ident())
            value = files.get(str(path))
            if value is None:
                raise FileNotFoundError(path)
            return StringIO(value)

        monkeypatch.setattr(subagent, "open", read, raising=False)
        monkeypatch.setattr(subagent.platform_compat, "IS_LINUX", True)
        calls: list[tuple[str, float | None]] = []
        clock = [0.0]
        notifier = ResourcePressureNotifier(
            NotificationBus(sink=lambda note: None),
            probe_fn=_ample,
            clock=lambda: clock[0],
            disk_fn=lambda: (POSTURE_AMPLE, "", None),
            psi_fn=lambda: 0.0,
        )
        notifier.memory_guard = lambda status, psi, trigger: calls.append((trigger, psi))

        async def scenario() -> int:
            for at, total in ((0.0, _us(100)), (30.0, _us(112))):
                files["/sys/fs/cgroup/memory.pressure"] = pressure(total)
                clock[0] = at
                await notifier.maybe_sample()
            return threading.get_ident()

        loop_thread = asyncio.run(scenario())
        assert calls == [("psi-cgroup", 40.0)]
        assert threads and loop_thread not in threads

    def test_a_guard_that_raises_does_not_break_sampling(self) -> None:
        clock = [0.0]
        notifier, _, probed, _ = _run(
            [(_status(POSTURE_CRITICAL), None, None), (_status(POSTURE_CRITICAL), None, None)],
            clock,
        )

        def boom(*_a: Any) -> None:
            raise RuntimeError("guard down")

        notifier.memory_guard = boom
        _sample_at(notifier, clock, [0.0, FAST_SAMPLE_INTERVAL_SECS])
        assert probed == [0.0, FAST_SAMPLE_INTERVAL_SECS]


class TestOneStallOneStop:
    def test_a_stall_the_stop_ends_stops_exactly_one_tree(self) -> None:
        # 37.5% stalled up to the stop at 5 s, part of the next 5 s while the tree
        # exits, then none. avg10 would still read about 25 at 10 s and 17 at 15 s.
        clock = [0.0]
        before = _us(0.375 * 5)
        notifier, calls, probed, _ = _run(
            [
                (_ample(), 0.0, 0),
                (_ample(), 0.0, before),
                (_ample(), 0.0, before + _us(1.0)),
                (_ample(), 0.0, before + _us(1.0)),
                (_ample(), 0.0, before + _us(1.0)),
            ],
            clock,
        )
        _sample_at(notifier, clock, [0.0, 5.0, 10.0, 15.0, 20.0])
        # 5 s hands off; 10 s only re-reads; 15 s and 20 s read no stall.
        assert probed == [0.0, 5.0, 10.0, 15.0, 20.0]
        assert calls == [("psi-cgroup", 37.5)]

    def test_a_stall_that_holds_stops_one_tree_per_reread(self) -> None:
        # Stalled 40% throughout: a stop at 5 s and at 15 s, each followed by a re-read.
        clock = [0.0]
        notifier, calls, probed, _ = _run(
            [(_ample(), 0.0, _us(2 * step)) for step in range(5)],
            clock,
        )
        _sample_at(notifier, clock, [0.0, 5.0, 10.0, 15.0, 20.0])
        assert probed == [0.0, 5.0, 10.0, 15.0, 20.0]
        assert calls == [("psi-cgroup", 40.0), ("psi-cgroup", 40.0)]

    def test_a_stop_still_running_holds_the_next_share(self) -> None:
        # The stop at 5 s still runs at 10 s (a tree that outlived SIGTERM), so 10 s is
        # no baseline and the stall it left behind stops nothing more. A new stall
        # once it has ended stops the next tree.
        clock = [0.0]
        notifier, calls, probed, _ = _run(
            [
                (_ample(), 0.0, 0),
                (_ample(), 0.0, _us(2)),
                (_ample(), 0.0, _us(4)),
                (_ample(), 0.0, _us(5.5)),
                (_ample(), 0.0, _us(5.5)),
                (_ample(), 0.0, _us(5.5)),
                (_ample(), 0.0, _us(7.5)),
            ],
            clock,
        )
        passes: list[asyncio.Future[None]] = []

        def guard(status: ResourceStatus, psi: float | None, trigger: str) -> object:
            calls.append((trigger, psi))
            passes.append(asyncio.get_running_loop().create_future())
            return passes[-1]

        notifier.memory_guard = guard

        async def scenario() -> None:
            for when in (0.0, 5.0, 10.0):
                clock[0] = when
                await notifier.maybe_sample()
            passes[0].set_result(None)
            for when in (15.0, 20.0, 25.0, 30.0):
                clock[0] = when
                await notifier.maybe_sample()

        asyncio.run(scenario())
        assert probed == [0.0, 5.0, 10.0, 15.0, 20.0, 25.0, 30.0]
        assert calls == [("psi-cgroup", 40.0), ("psi-cgroup", 40.0)]

    def test_a_stop_that_ends_during_the_probe_leaves_no_baseline(self) -> None:
        # The stop at 5 s ends after the worker read the counter at 10 s but before
        # the loop resumed; that reading was taken while the tree still stalled.
        clock = [0.0]
        notifier, calls, probed, _ = _run(
            [
                (_ample(), 0.0, 0),
                (_ample(), 0.0, _us(2)),
                (_ample(), 0.0, _us(4)),
                (_ample(), 0.0, _us(5.5)),
                (_ample(), 0.0, _us(5.5)),
            ],
            clock,
        )
        passes: list[asyncio.Future[None]] = []

        def guard(status: ResourceStatus, psi: float | None, trigger: str) -> object:
            calls.append((trigger, psi))
            passes.append(asyncio.get_running_loop().create_future())
            return passes[-1]

        notifier.memory_guard = guard
        read_stall = notifier._view_stall_fn

        async def scenario() -> None:
            loop = asyncio.get_running_loop()

            def stall_then_end_the_stop() -> int | None:
                total = read_stall()
                if clock[0] == 10.0:
                    loop.call_soon_threadsafe(passes[0].set_result, None)
                return total

            notifier._view_stall_fn = stall_then_end_the_stop
            for when in (0.0, 5.0, 10.0, 15.0, 20.0):
                clock[0] = when
                await notifier.maybe_sample()

        asyncio.run(scenario())
        assert passes[0].done()
        assert probed == [0.0, 5.0, 10.0, 15.0, 20.0]
        assert calls == [("psi-cgroup", 40.0)]

    def test_a_hand_off_on_another_trigger_drops_the_baseline_too(self) -> None:
        # A host-PSI stop on a tight reading at 5 s: the stall behind it must not stop
        # a second tree at 10 s.
        clock = [0.0]
        notifier, calls, probed, _ = _run(
            [
                (_ample(), 0.0, 0),
                (_status(POSTURE_TIGHT, gb=3.0), 40.0, _us(2)),
                (_ample(), 0.0, _us(4)),
                (_ample(), 0.0, _us(4)),
            ],
            clock,
        )
        _sample_at(notifier, clock, [0.0, 5.0, 10.0, 15.0])
        assert probed == [0.0, 5.0, 10.0, 15.0]
        assert calls == [("psi", 40.0)]


class TestFastSampling:
    def test_tight_and_critical_sample_every_beat_then_ample_returns_to_30s(self) -> None:
        clock = [0.0]
        notifier, _, probed, _ = _run(
            [
                (_status(POSTURE_TIGHT, gb=3.0), None, None),
                (_status(POSTURE_CRITICAL), None, None),
                (_status(POSTURE_AMPLE, gb=20.0), None, None),
                (_status(POSTURE_AMPLE, gb=20.0), None, None),
            ],
            clock,
        )
        _sample_at(notifier, clock, [0.0, 5.0, 10.0, 15.0, 20.0, 40.0])
        # tight at 0 -> next at 5 (critical) -> next at 10 (ample) -> gated to 40.
        assert FAST_SAMPLE_INTERVAL_SECS == 5.0
        assert probed == [0.0, 5.0, 10.0, 10.0 + SAMPLE_INTERVAL_SECS]

    def test_while_out_of_view_the_stall_is_sampled_every_beat(self) -> None:
        # The free figure never turns tight there, so the stall is the only warning and
        # must not wait 30 s. With no view reading, ample is back to 30 s.
        clock = [0.0]
        notifier, calls, probed, _ = _run(
            [(_ample(), 0.0, 0), (_ample(), 0.0, 0), (_ample(), 0.0, None), (_ample(), 0.0, None)],
            clock,
        )
        _sample_at(notifier, clock, [0.0, 5.0, 10.0, 15.0, 40.0])
        assert probed == [0.0, 5.0, 10.0, 10.0 + SAMPLE_INTERVAL_SECS]
        assert calls == []

    def test_with_no_guard_wired_an_ample_sample_reads_no_view_and_keeps_30s(self) -> None:
        clock = [0.0]
        notifier, _, probed, viewed = _run(
            [(_ample(), 0.0, 0), (_ample(), 0.0, _us(12))],
            clock,
        )
        notifier.memory_guard = None
        _sample_at(notifier, clock, [0.0, 5.0, SAMPLE_INTERVAL_SECS])
        assert probed == [0.0, SAMPLE_INTERVAL_SECS]
        assert viewed == []


class TestStallShare:
    @pytest.mark.parametrize(
        ("before", "after", "expected"),
        [
            ((0.0, 0), (5.0, _us(2)), 40.0),
            ((10.0, _us(1)), (40.0, _us(4)), 10.0),
            ((0.0, 0), (5.0, 0), 0.0),
            # Read jitter can put a share a little over 100; it is capped.
            ((0.0, 0), (5.0, _us(5.2)), 100.0),
            (None, (5.0, _us(2)), None),  # no baseline yet
            ((0.0, 0), None, None),  # no reading now
            ((5.0, 0), (5.0, _us(1)), None),  # no time passed
            ((5.0, 0), (4.0, _us(1)), None),
            ((0.0, _us(9)), (5.0, _us(1)), None),  # a new cgroup counts from zero again
        ],
    )
    def test_share_of_time_stalled(
        self,
        before: tuple[float, int] | None,
        after: tuple[float, int] | None,
        expected: float | None,
    ) -> None:
        assert resource_pressure.stall_share(before, after) == expected


class TestPsiReader:
    def test_reads_full_avg10(self, tmp_path: Path) -> None:
        path = tmp_path / "memory"
        path.write_text(
            "some avg10=31.20 avg60=10.00 avg300=2.00 total=1\n"
            "full avg10=12.50 avg60=4.00 avg300=1.00 total=2\n"
        )
        assert linux_memory_psi_full_avg10(str(path)) == 12.5

    def test_missing_or_malformed_file_reads_none(self, tmp_path: Path) -> None:
        assert linux_memory_psi_full_avg10(str(tmp_path / "absent")) is None
        bad = tmp_path / "bad"
        bad.write_text("full avg10=nope\n")
        assert linux_memory_psi_full_avg10(str(bad)) is None
        some_only = tmp_path / "some"
        some_only.write_text("some avg10=5.00 avg60=1.00 avg300=0.00 total=1\n")
        assert linux_memory_psi_full_avg10(str(some_only)) is None
