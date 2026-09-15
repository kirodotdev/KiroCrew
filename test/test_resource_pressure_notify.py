"""Tests for the resource-pressure notification producer.

Exercises the episode/hysteresis state machine in
:mod:`kiro_crew.notifications.resource_pressure` against a real
:class:`NotificationBus` (list sink) with injected probes and clock — no real
``/proc`` or wall-clock dependence.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest

from kiro_crew.notifications import resource_pressure
from kiro_crew.notifications.bus import SYSTEM_CHANNELS, NotificationBus
from kiro_crew.notifications.resource_pressure import (
    CHANNEL,
    CRITICAL_REALERT_SECS,
    SAMPLE_INTERVAL_SECS,
    SUSTAINED_TIGHT_SECS,
    ResourcePressureNotifier,
)
from kiro_crew.resource_status import (
    POSTURE_AMPLE,
    POSTURE_CRITICAL,
    POSTURE_TIGHT,
    POSTURE_UNKNOWN,
    ResourceStatus,
)


def _status(posture: str, gb: float = 1.5, load: float | None = 0.5) -> ResourceStatus:
    return ResourceStatus(
        available_gb=gb,
        cpu_count=8,
        load_per_cpu=load,
        posture=posture,
        pressure_gb=4.0,
        critical_gb=2.0,
    )


@pytest.fixture
def notes() -> list[dict[str, Any]]:
    return []


@pytest.fixture
def notifier(notes: list[dict[str, Any]]) -> ResourcePressureNotifier:
    bus = NotificationBus(sink=notes.append)
    return ResourcePressureNotifier(bus)


class TestChannel:
    def test_system_resources_is_a_registered_system_channel(self) -> None:
        assert CHANNEL in SYSTEM_CHANNELS
        assert SYSTEM_CHANNELS[CHANNEL] == "default"


class TestSliceOomNote:
    """A slice OOM report captured off-loop is pushed onto the bus from the
    loop side — 'a random subagent died (137)' must be diagnosable from the
    dashboard, not just the log file."""

    @pytest.mark.asyncio
    async def test_oom_report_reaches_the_bus(self, notes, monkeypatch) -> None:
        bus = NotificationBus(sink=notes.append)
        notifier = ResourcePressureNotifier(
            bus, probe_fn=lambda: _status(POSTURE_AMPLE, gb=20.0)
        )
        monkeypatch.setattr(
            resource_pressure,
            "check_agents_slice_pressure",
            lambda: "cgroup OOM kill inside kirocrew-agents.slice: 1 new kill(s); …",
        )
        await notifier.maybe_sample()
        oom = [n for n in notes if n.get("group_key") == "agents-slice-oom"]
        assert len(oom) == 1
        assert "OOM" in oom[0]["title"]
        assert "1 new kill(s)" in oom[0]["body"]

    @pytest.mark.asyncio
    async def test_quiet_when_no_kills(self, notes, monkeypatch) -> None:
        bus = NotificationBus(sink=notes.append)
        notifier = ResourcePressureNotifier(
            bus, probe_fn=lambda: _status(POSTURE_AMPLE, gb=20.0)
        )
        monkeypatch.setattr(
            resource_pressure, "check_agents_slice_pressure", lambda: None
        )
        await notifier.maybe_sample()
        assert [n for n in notes if n.get("group_key") == "agents-slice-oom"] == []


class TestCriticalEpisode:
    def test_entering_critical_pushes_one_critical_note(self, notifier, notes) -> None:
        notifier.observe(_status(POSTURE_CRITICAL, gb=1.2), now=0.0)
        assert len(notes) == 1
        note = notes[0]
        assert note["channel"] == CHANNEL
        assert note["priority"] == "critical"
        assert "1.2 GB" in note["body"]
        assert "load 0.5/core" in note["body"]

    def test_staying_critical_is_quiet_within_the_realert_interval(
        self, notifier, notes
    ) -> None:
        for t in range(0, int(CRITICAL_REALERT_SECS), 30):
            notifier.observe(_status(POSTURE_CRITICAL), now=float(t))
        assert len(notes) == 1

    def test_sustained_critical_realerts_on_the_cadence(self, notifier, notes) -> None:
        two_intervals = int(CRITICAL_REALERT_SECS * 2)
        for t in range(0, two_intervals + 30, 30):
            notifier.observe(_status(POSTURE_CRITICAL), now=float(t))
        assert len(notes) == 3  # entry + one re-alert per elapsed interval
        assert notes[1]["title"].startswith("Host memory still critically low")
        assert {n["priority"] for n in notes} == {"critical"}
        assert {n["group_key"] for n in notes} == {"resource-pressure"}

    def test_recovery_resets_the_realert_timer(self, notifier, notes) -> None:
        notifier.observe(_status(POSTURE_CRITICAL), now=0.0)
        notifier.observe(_status(POSTURE_AMPLE, gb=12.0), now=30.0)
        start = CRITICAL_REALERT_SECS + 60.0  # new episode past the old cadence
        notifier.observe(_status(POSTURE_CRITICAL), now=start)
        notifier.observe(_status(POSTURE_CRITICAL), now=start + 30.0)
        # entry, recovery, new entry — no spurious re-alert from the old timer
        assert len(notes) == 3

    def test_oscillation_across_the_cadence_boundary_does_not_realert(
        self, notifier, notes
    ) -> None:
        # critical<->tight flapping for hours: every tight reading interrupts
        # "continuously critical", so the cadence never elapses and the
        # episode dedupe promise holds.
        end = int(CRITICAL_REALERT_SECS * 4)
        for t in range(0, end, 30):
            posture = POSTURE_CRITICAL if (t // 30) % 2 == 0 else POSTURE_TIGHT
            notifier.observe(_status(posture, gb=3.0), now=float(t))
        assert len(notes) == 1  # the entry note only

    def test_realert_measures_continuous_critical_not_wall_time(
        self, notifier, notes
    ) -> None:
        notifier.observe(_status(POSTURE_CRITICAL), now=0.0)
        # dip to tight just before the cadence would elapse
        notifier.observe(_status(POSTURE_TIGHT, gb=3.0), now=CRITICAL_REALERT_SECS - 30.0)
        back = CRITICAL_REALERT_SECS + 30.0
        notifier.observe(_status(POSTURE_CRITICAL), now=back)
        assert len(notes) == 1  # cadence restarted at the tight dip
        # ...and a full continuous-critical interval after that DOES re-alert
        notifier.observe(_status(POSTURE_CRITICAL), now=back + CRITICAL_REALERT_SECS)
        assert len(notes) == 2

    def test_critical_tight_oscillation_does_not_realert(self, notifier, notes) -> None:
        notifier.observe(_status(POSTURE_CRITICAL), now=0.0)
        notifier.observe(_status(POSTURE_TIGHT, gb=3.0), now=30.0)
        notifier.observe(_status(POSTURE_CRITICAL), now=60.0)
        notifier.observe(_status(POSTURE_TIGHT, gb=3.0), now=90.0)
        assert len(notes) == 1

    def test_recovery_after_critical_pushes_recovery_note(self, notifier, notes) -> None:
        notifier.observe(_status(POSTURE_CRITICAL), now=0.0)
        notifier.observe(_status(POSTURE_AMPLE, gb=12.0), now=30.0)
        assert len(notes) == 2
        assert "resolved" in notes[1]["title"]
        assert "12.0 GB" in notes[1]["body"]

    def test_new_episode_after_recovery_alerts_again(self, notifier, notes) -> None:
        notifier.observe(_status(POSTURE_CRITICAL), now=0.0)
        notifier.observe(_status(POSTURE_AMPLE, gb=12.0), now=30.0)
        notifier.observe(_status(POSTURE_CRITICAL), now=60.0)
        assert len(notes) == 3
        assert notes[2]["priority"] == "critical"


class TestSustainedTight:
    def test_brief_tight_is_silent(self, notifier, notes) -> None:
        notifier.observe(_status(POSTURE_TIGHT, gb=3.5), now=0.0)
        notifier.observe(_status(POSTURE_TIGHT, gb=3.5), now=SUSTAINED_TIGHT_SECS - 1)
        assert notes == []

    def test_brief_tight_recovering_is_fully_silent(self, notifier, notes) -> None:
        # No alert fired -> no recovery note either (a blip must not chat).
        notifier.observe(_status(POSTURE_TIGHT), now=0.0)
        notifier.observe(_status(POSTURE_AMPLE, gb=12.0), now=60.0)
        assert notes == []

    def test_sustained_tight_pushes_one_default_note(self, notifier, notes) -> None:
        notifier.observe(_status(POSTURE_TIGHT, gb=3.5), now=0.0)
        notifier.observe(_status(POSTURE_TIGHT, gb=3.5), now=SUSTAINED_TIGHT_SECS)
        assert len(notes) == 1
        note = notes[0]
        assert note["priority"] == "default"
        assert "3.5 GB" in note["body"]
        # Continuing tight stays silent.
        notifier.observe(_status(POSTURE_TIGHT), now=SUSTAINED_TIGHT_SECS + 300)
        assert len(notes) == 1

    def test_sustained_tight_then_recovery_note(self, notifier, notes) -> None:
        notifier.observe(_status(POSTURE_TIGHT), now=0.0)
        notifier.observe(_status(POSTURE_TIGHT), now=SUSTAINED_TIGHT_SECS)
        notifier.observe(_status(POSTURE_AMPLE, gb=12.0), now=SUSTAINED_TIGHT_SECS + 30)
        assert len(notes) == 2
        assert "resolved" in notes[1]["title"]

    def test_escalation_to_critical_fires_after_tight_note(self, notifier, notes) -> None:
        # A sustained-tight note must not muffle a later entering-critical
        # alert -- "about to freeze" is strictly more urgent news.
        notifier.observe(_status(POSTURE_TIGHT), now=0.0)
        notifier.observe(_status(POSTURE_TIGHT), now=SUSTAINED_TIGHT_SECS)
        notifier.observe(_status(POSTURE_CRITICAL, gb=1.0), now=SUSTAINED_TIGHT_SECS + 30)
        assert len(notes) == 2
        assert notes[1]["priority"] == "critical"

    def test_critical_first_suppresses_the_tight_note(self, notifier, notes) -> None:
        # De-escalation critical -> tight is weaker news; even sustained,
        # the tight note stays suppressed within the episode.
        notifier.observe(_status(POSTURE_CRITICAL), now=0.0)
        notifier.observe(_status(POSTURE_TIGHT), now=30.0)
        notifier.observe(_status(POSTURE_TIGHT), now=SUSTAINED_TIGHT_SECS + 60)
        assert len(notes) == 1  # just the critical entry


class TestUnknownPosture:
    def test_unknown_never_starts_an_episode(self, notifier, notes) -> None:
        notifier.observe(_status(POSTURE_UNKNOWN, gb=-1.0), now=0.0)
        notifier.observe(_status(POSTURE_UNKNOWN, gb=-1.0), now=30.0)
        assert notes == []

    def test_unknown_does_not_close_a_critical_episode(self, notifier, notes) -> None:
        notifier.observe(_status(POSTURE_CRITICAL), now=0.0)
        notifier.observe(_status(POSTURE_UNKNOWN, gb=-1.0), now=30.0)
        notifier.observe(_status(POSTURE_CRITICAL), now=60.0)
        # No false recovery, no re-alert.
        assert len(notes) == 1

    def test_unknown_does_not_reset_the_tight_timer(self, notifier, notes) -> None:
        notifier.observe(_status(POSTURE_TIGHT), now=0.0)
        notifier.observe(_status(POSTURE_UNKNOWN, gb=-1.0), now=300.0)
        notifier.observe(_status(POSTURE_TIGHT), now=SUSTAINED_TIGHT_SECS)
        assert len(notes) == 1  # timer origin survived the probe gap


class TestMaybeSample:
    def test_gates_to_the_sample_interval(self, notes) -> None:
        bus = NotificationBus(sink=notes.append)
        calls: list[float] = []
        clock_now = [0.0]

        def fake_probe() -> ResourceStatus:
            calls.append(clock_now[0])
            return _status(POSTURE_AMPLE, gb=12.0)

        notifier = ResourcePressureNotifier(bus, probe_fn=fake_probe, clock=lambda: clock_now[0])

        async def scenario() -> None:
            await notifier.maybe_sample()  # first call probes immediately
            clock_now[0] = 5.0
            await notifier.maybe_sample()  # inside the interval -> gated
            clock_now[0] = SAMPLE_INTERVAL_SECS
            await notifier.maybe_sample()

        asyncio.run(scenario())
        assert calls == [0.0, SAMPLE_INTERVAL_SECS]

    def test_probe_runs_off_the_event_loop(self, notes) -> None:
        bus = NotificationBus(sink=notes.append)
        loop_thread: list[Any] = []

        def probe_recording_thread() -> ResourceStatus:
            loop_thread.append(threading.current_thread())
            return _status(POSTURE_AMPLE, gb=12.0)

        notifier = ResourcePressureNotifier(
            bus, probe_fn=probe_recording_thread, clock=lambda: 100.0
        )

        async def scenario() -> None:
            await notifier.maybe_sample()

        asyncio.run(scenario())
        assert loop_thread and loop_thread[0] is not threading.main_thread()

    def test_stalled_probe_forfeits_the_sample_instead_of_hanging(
        self, notes, monkeypatch
    ) -> None:
        monkeypatch.setattr(resource_pressure, "PROBE_TIMEOUT_SECS", 0.05)
        bus = NotificationBus(sink=notes.append)
        release = threading.Event()

        def stalled_probe() -> ResourceStatus:
            release.wait(5.0)
            return _status(POSTURE_CRITICAL)

        notifier = ResourcePressureNotifier(bus, probe_fn=stalled_probe, clock=lambda: 100.0)

        async def scenario() -> None:
            await notifier.maybe_sample()  # must return promptly, not raise

        asyncio.run(asyncio.wait_for(scenario(), timeout=2.0))
        assert notes == []  # stalled sample forfeited
        release.set()  # let the abandoned worker thread finish

    def test_inflight_probe_blocks_a_second_probe(self, notes) -> None:
        bus = NotificationBus(sink=notes.append)
        calls: list[float] = []
        clock_now = [0.0]

        def counting_probe() -> ResourceStatus:
            calls.append(clock_now[0])
            return _status(POSTURE_AMPLE, gb=12.0)

        notifier = ResourcePressureNotifier(bus, probe_fn=counting_probe, clock=lambda: clock_now[0])
        notifier._inflight = True  # simulate a probe still occupying the slot
        clock_now[0] = SAMPLE_INTERVAL_SECS * 3

        async def scenario() -> None:
            await notifier.maybe_sample()

        asyncio.run(scenario())
        assert calls == []  # gated by the in-flight slot, not just the interval

    def test_never_raises_when_the_probe_explodes(self, notes) -> None:
        bus = NotificationBus(sink=notes.append)

        def bad_probe() -> ResourceStatus:
            raise RuntimeError("boom")

        notifier = ResourcePressureNotifier(bus, probe_fn=bad_probe, clock=lambda: 100.0)
        asyncio.run(notifier.maybe_sample())  # must not raise
        assert notes == []

    def test_never_raises_when_the_sink_explodes(self) -> None:
        def bad_sink(note: dict[str, Any]) -> None:
            raise RuntimeError("sink down")

        bus = NotificationBus(sink=bad_sink)
        notifier = ResourcePressureNotifier(
            bus, probe_fn=lambda: _status(POSTURE_CRITICAL), clock=lambda: 100.0
        )
        asyncio.run(notifier.maybe_sample())  # must not raise


class TestNoteShape:
    def test_notes_group_under_one_key_and_carry_meta(self, notifier, notes) -> None:
        notifier.observe(_status(POSTURE_CRITICAL, gb=1.2), now=0.0)
        notifier.observe(_status(POSTURE_AMPLE, gb=12.0), now=30.0)
        assert {n["group_key"] for n in notes} == {"resource-pressure"}
        assert notes[0]["posture"] == POSTURE_CRITICAL
        assert notes[0]["available_gb"] == 1.2

    def test_missing_load_signal_is_omitted_from_the_body(self, notifier, notes) -> None:
        notifier.observe(_status(POSTURE_CRITICAL, load=None), now=0.0)
        assert "load" not in notes[0]["body"]
