"""The adaptive controller's gateway.log trail: what ends a throttled period,
and what the loop was running when loop lag caused a cut.

gateway.log keeps WARNING and above. A cut is logged there and each growth step
stays at INFO, so a period's end, and so its length, needs its own WARNING. A
loop_lag cut also names the loop thread's frame, read by ``LoopBusySampler``
while the controller's timer was late, so the cut's cause can be read back.
"""

from __future__ import annotations

import asyncio.events
import json
import logging
import sys
import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from test_adaptive_controller import FakeGate, FakeManager, _controller

from kiro_crew.adaptive import controller as ctl_mod
from kiro_crew.adaptive.busy import LoopBusySampler, describe_frame
from kiro_crew.adaptive.policy import (
    ACTION_DECREASE,
    ACTION_FIXED,
    ACTION_HOLD,
    ACTION_INCREASE,
    ACTION_PAUSE,
    ACTION_RESUME,
    Decision,
)
from kiro_crew.adaptive.signals import SIGNAL_LOOP_LAG, Sample


def _frame(module: str, qualname: str, back: Any = None) -> Any:
    return SimpleNamespace(
        f_globals={"__name__": module},
        f_code=SimpleNamespace(co_qualname=qualname, co_name=qualname.rsplit(".", 1)[-1]),
        f_back=back,
    )


def _handle_frame(callback: Any, back: Any) -> Any:
    """asyncio's ``Handle._run`` frame, running *callback*."""
    return SimpleNamespace(
        f_globals={"__name__": "asyncio.events"},
        f_code=asyncio.events.Handle._run.__code__,
        f_locals={"self": SimpleNamespace(_callback=callback)},
        f_back=back,
    )


def _loop_frames_from(entry: Any) -> Any:
    """The event loop's own frames, started from *entry*."""
    run_forever = _frame("asyncio.base_events", "BaseEventLoop.run_forever", entry)
    return _frame("asyncio.base_events", "BaseEventLoop._run_once", run_forever)


def _decision(exec_cap: int, gate_cap: int, action: str, **over: Any) -> Decision:
    base: dict[str, Any] = dict(
        effective_exec_cap=exec_cap,
        spawn_gate_capacity=gate_cap,
        paused=False,
        probing=False,
        action=action,
        reason=f"synthetic {action}",
        changed=action != ACTION_HOLD,
    )
    base.update(over)
    return Decision(**base)


def _messages(caplog: pytest.LogCaptureFixture, level: int) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == level and r.getMessage().startswith("adaptive concurrency")
    ]


class TestDescribeFrame:
    def test_a_library_frame_is_named_with_the_package_frame_below_it(self) -> None:
        own = _frame("kiro_crew.shell_normalizer", "_fold_line_continuations")
        lib = _frame("json.decoder", "JSONDecoder.raw_decode", back=_frame("json", "loads", own))
        assert describe_frame(lib) == (
            "json.decoder.JSONDecoder.raw_decode via kiro_crew.shell_normalizer._fold_line_continuations"
        )

    def test_a_package_frame_is_named_alone(self) -> None:
        assert describe_frame(_frame("kiro_crew.agent", "rebuild")) == "kiro_crew.agent.rebuild"

    def test_a_native_callback_under_the_loop_handle_is_named(self) -> None:
        """A native callback has no Python frame of its own, so the innermost
        frame is asyncio's ``Handle._run``; the reading names the callback the
        handle is running, not the program's entry point further out."""
        lock = threading.Lock()
        handle = _handle_frame(
            lock.acquire, back=_loop_frames_from(_frame("kiro_crew.cli", "main"))
        )
        assert describe_frame(handle) == "asyncio.events.Handle._run via _thread.lock.acquire"

    def test_a_library_callback_is_not_named_by_the_entry_point(self) -> None:
        """Frames beyond ``Handle._run`` are the loop and whatever started it,
        not what held the loop: the walk stops at the handle and names its callback."""
        handle = _handle_frame(json.loads, back=_loop_frames_from(_frame("kiro_crew.cli", "main")))
        lib = _frame("json.decoder", "JSONDecoder.raw_decode", back=_frame("json", "loads", handle))
        assert describe_frame(lib) == "json.decoder.JSONDecoder.raw_decode via json.loads"

    def test_a_real_frame_names_code_and_no_path(self) -> None:
        def held_the_loop() -> Any:
            return sys._getframe()

        text = describe_frame(held_the_loop())
        assert text.endswith("held_the_loop")
        assert "/" not in text and "\\" not in text and ".py" not in text

    def test_each_part_is_bounded(self) -> None:
        assert len(describe_frame(_frame("m", "f" * 500))) <= 120

    def test_no_frame_is_empty(self) -> None:
        assert describe_frame(None) == ""


class TestLoopBusySampler:
    def test_a_late_loop_is_read_while_it_is_still_busy(self) -> None:
        """The "loop" here is this test thread, held until the sampler has read
        its frame: the frame reader releases it, so the read happens while the
        thread is still inside the blocking function, on any host speed."""
        read = threading.Event()
        seen: list[str] = []
        loop_thread = threading.get_ident()

        def frames() -> dict[int, Any]:
            current = sys._current_frames()
            frame = current.get(loop_thread)
            while frame is not None:
                seen.append(frame.f_code.co_name)
                frame = frame.f_back
            read.set()
            return current

        sampler = LoopBusySampler(frames=frames)
        sampler.start(loop_thread)
        try:

            def blocks_the_loop() -> bool:
                return read.wait(timeout=30)

            sampler.arm(0.0)
            assert blocks_the_loop()
            busy = sampler.disarm()
        finally:
            sampler.stop()
        # Read while this thread was inside the blocking function; the innermost
        # frame there is the library wait it is parked in.
        assert "blocks_the_loop" in seen
        assert busy == "threading.Condition.wait"

    def test_a_punctual_loop_reads_no_frame(self) -> None:
        reads: list[int] = []
        sampler = LoopBusySampler(frames=lambda: reads.append(1) or {})
        sampler.start(threading.get_ident())
        try:
            sampler.arm(5.0)
            assert sampler.disarm() == ""
        finally:
            sampler.stop()
        assert reads == []

    @pytest.mark.asyncio
    async def test_the_controller_stops_the_sampler_off_the_loop(self) -> None:
        """Stopping joins the sampler's thread, so it must not run on the loop."""
        mgr = FakeManager(user_max=10)
        ctl, _clock = _controller(mgr)
        stopped_on: list[int] = []

        class _Sampler:
            def stop(self) -> None:
                stopped_on.append(threading.get_ident())

        ctl._busy_sampler = _Sampler()  # type: ignore[assignment]
        await ctl.stop()
        assert stopped_on and stopped_on[0] != threading.get_ident()


class TestThrottledPeriod:
    @pytest.mark.asyncio
    async def test_the_climb_back_to_the_pre_cut_caps_is_one_warning(self, caplog) -> None:
        mgr = FakeManager(user_max=10)
        ctl, clock = _controller(mgr)
        with caplog.at_level(logging.INFO, logger=ctl_mod.__name__):
            await ctl.apply(_decision(10, 4, ACTION_HOLD))
            await ctl.apply(_decision(5, 4, ACTION_DECREASE))
            clock.advance(42)
            await ctl.apply(_decision(8, 4, ACTION_INCREASE))
            assert not any("restored" in m for m in _messages(caplog, logging.WARNING))
            clock.advance(18)
            await ctl.apply(_decision(10, 4, ACTION_INCREASE))
            await ctl.apply(_decision(10, 4, ACTION_INCREASE, changed=False))
        restored = [m for m in _messages(caplog, logging.WARNING) if "restored" in m]
        assert restored == [
            "adaptive concurrency restored: exec_cap=10 gate_cap=4 after 60s "
            "(before the cut: exec_cap=10 gate_cap=4)"
        ]
        # Each growth step stays at INFO; the unchanged repeat logs nothing.
        assert len([m for m in _messages(caplog, logging.INFO) if " increase:" in m]) == 2

    @pytest.mark.asyncio
    async def test_a_paused_gate_ends_its_period_only_once_it_climbs_back(self, caplog) -> None:
        mgr = FakeManager(user_max=10)
        ctl, clock = _controller(mgr, FakeGate())
        with caplog.at_level(logging.WARNING, logger=ctl_mod.__name__):
            await ctl.apply(_decision(10, 6, ACTION_HOLD))
            await ctl.apply(_decision(10, 1, ACTION_PAUSE, paused=True))
            clock.advance(30)
            await ctl.apply(_decision(10, 1, ACTION_RESUME))
            await ctl.apply(_decision(10, 4, ACTION_INCREASE))
            assert not any("restored" in m for m in _messages(caplog, logging.WARNING))
            clock.advance(30)
            await ctl.apply(_decision(10, 6, ACTION_INCREASE))
        restored = [m for m in _messages(caplog, logging.WARNING) if "restored" in m]
        assert len(restored) == 1 and "gate_cap=6 after 60s" in restored[0]

    @pytest.mark.asyncio
    async def test_a_lowered_ceiling_lowers_the_target(self, caplog) -> None:
        mgr = FakeManager(user_max=10)
        ctl, clock = _controller(mgr)
        with caplog.at_level(logging.WARNING, logger=ctl_mod.__name__):
            await ctl.apply(_decision(10, 4, ACTION_HOLD))
            await ctl.apply(_decision(5, 4, ACTION_DECREASE))
            mgr._user = 6
            await ctl.apply(_decision(6, 4, ACTION_INCREASE))
        assert any("restored: exec_cap=6" in m for m in _messages(caplog, logging.WARNING))

    @pytest.mark.asyncio
    async def test_a_cut_on_the_first_decision_opens_a_period(self, caplog) -> None:
        """Before any decision the policy holds its start caps, which is what
        the first cut is measured against."""
        mgr = FakeManager(user_max=10)
        ctl, clock = _controller(mgr, FakeGate())
        start = (ctl._policy.params.exec_start, ctl._policy.params.gate_start)
        with caplog.at_level(logging.WARNING, logger=ctl_mod.__name__):
            await ctl.apply(_decision(start[0], start[1] - 2, ACTION_DECREASE))
            clock.advance(12)
            await ctl.apply(_decision(start[0], start[1], ACTION_INCREASE))
        restored = [m for m in _messages(caplog, logging.WARNING) if "restored" in m]
        assert len(restored) == 1 and "after 12s" in restored[0]

    @pytest.mark.asyncio
    async def test_an_idle_return_to_the_start_caps_ends_the_period(self, caplog) -> None:
        """Demand grew the gate past its start before the cut; with no demand
        left the policy only restores to the start, which ends the period."""
        mgr = FakeManager(user_max=10)
        ctl, clock = _controller(mgr, FakeGate())
        gate_start = ctl._policy.params.gate_start
        with caplog.at_level(logging.WARNING, logger=ctl_mod.__name__):
            await ctl.apply(_decision(10, gate_start + 4, ACTION_HOLD))
            await ctl.apply(_decision(10, gate_start, ACTION_DECREASE))
            ctl._samples.append(Sample(t=clock.t, running=2, queued=1))
            await ctl.apply(_decision(10, gate_start, ACTION_HOLD))
            assert not any("restored" in m for m in _messages(caplog, logging.WARNING))
            clock.advance(40)
            ctl._samples.append(Sample(t=clock.t, running=0, queued=0))
            await ctl.apply(_decision(10, gate_start, ACTION_HOLD))
        restored = [m for m in _messages(caplog, logging.WARNING) if "restored" in m]
        assert len(restored) == 1 and "after 40s" in restored[0]

    @pytest.mark.asyncio
    async def test_a_switch_to_fixed_mode_ends_the_period(self, caplog) -> None:
        mgr = FakeManager(user_max=10)
        ctl, clock = _controller(mgr, FakeGate())
        with caplog.at_level(logging.WARNING, logger=ctl_mod.__name__):
            await ctl.apply(_decision(10, 8, ACTION_HOLD))
            await ctl.apply(_decision(5, 4, ACTION_DECREASE))
            await ctl.apply(_decision(10, 4, ACTION_FIXED))
        assert any("restored" in m for m in _messages(caplog, logging.WARNING))


class TestLoopLagCutNamesTheBusyFrame:
    @pytest.mark.asyncio
    async def test_a_loop_lag_cut_names_the_frame(self, caplog) -> None:
        mgr = FakeManager(user_max=10)
        ctl, _clock = _controller(mgr, FakeGate())
        ctl._last_busy = "kiro_crew.shell_normalizer._fold_line_continuations"
        with caplog.at_level(logging.WARNING, logger=ctl_mod.__name__):
            await ctl.apply(_decision(10, 6, ACTION_HOLD))
            await ctl.apply(_decision(10, 3, ACTION_DECREASE, signals=(SIGNAL_LOOP_LAG,)))
        cut = [m for m in _messages(caplog, logging.WARNING) if " decrease:" in m]
        assert cut and cut[0].endswith(" busy=kiro_crew.shell_normalizer._fold_line_continuations")

    @pytest.mark.asyncio
    async def test_a_cut_from_another_signal_does_not_name_it(self, caplog) -> None:
        mgr = FakeManager(user_max=10)
        ctl, _clock = _controller(mgr)
        ctl._last_busy = "kiro_crew.agent.rebuild"
        with caplog.at_level(logging.WARNING, logger=ctl_mod.__name__):
            await ctl.apply(_decision(10, 4, ACTION_HOLD))
            await ctl.apply(_decision(5, 4, ACTION_DECREASE, signals=("work_timeouts",)))
        assert not any("busy=" in m for m in _messages(caplog, logging.WARNING))

    @pytest.mark.asyncio
    async def test_the_sample_cycle_arms_the_sampler_and_hands_its_reading_on(self) -> None:
        mgr = FakeManager(user_max=10)
        ctl, _clock = _controller(mgr)

        class _Sampler:
            def __init__(self) -> None:
                self.armed: list[float] = []

            def arm(self, after_secs: float) -> None:
                self.armed.append(after_secs)

            def disarm(self) -> str:
                return "kiro_crew.agent.rebuild"

        sampler = _Sampler()
        ctl._busy_sampler = sampler  # type: ignore[assignment]
        ctl._sleep = AsyncMock()
        seen: dict[str, Any] = {}

        async def _tick(**kw: Any) -> None:
            seen.update(kw)

        ctl.tick = _tick  # type: ignore[method-assign]
        await ctl._sample_and_tick()
        lag_s = ctl._policy.params.thresholds.lag_decrease_ms / 1000.0
        assert sampler.armed == [pytest.approx(ctl._sample_secs + lag_s)]
        assert seen["loop_busy"] == "kiro_crew.agent.rebuild"

    @pytest.mark.asyncio
    async def test_tick_keeps_the_reading_for_the_decision(self) -> None:
        mgr = FakeManager(user_max=10)
        ctl, _clock = _controller(mgr)
        await ctl.tick(loop_lag_ms=0.0, loop_busy="kiro_crew.agent.rebuild")
        assert ctl._last_busy == "kiro_crew.agent.rebuild"
        await ctl.tick(loop_lag_ms=0.0)
        assert ctl._last_busy == ""
