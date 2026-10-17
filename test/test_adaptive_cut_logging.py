"""The adaptive controller's gateway.log trail: what ends a throttled period,
and what the loop was running when loop lag caused a cut.

gateway.log keeps WARNING and above. A cut is logged there and each growth step
stays at INFO, so a period's end, and so its length, needs its own WARNING. A
loop_lag cut also names the loop thread's frame, read by ``LoopBusySampler``
while the controller's timer was late, so the cut's cause can be read back.
"""

from __future__ import annotations

import logging
import threading
import time
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
from kiro_crew.adaptive.signals import SIGNAL_LOOP_LAG


def _frame(module: str, qualname: str, back: Any = None) -> Any:
    return SimpleNamespace(
        f_globals={"__name__": module},
        f_code=SimpleNamespace(co_qualname=qualname, co_name=qualname.rsplit(".", 1)[-1]),
        f_back=back,
    )


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

    def test_a_real_frame_names_code_and_no_path(self) -> None:
        import sys

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
        sampler = LoopBusySampler()
        sampler.start(threading.get_ident())
        try:

            def blocks_the_loop() -> None:
                time.sleep(0.4)

            sampler.arm(0.05)
            blocks_the_loop()
            busy = sampler.disarm()
        finally:
            sampler.stop()
        assert "blocks_the_loop" in busy

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
