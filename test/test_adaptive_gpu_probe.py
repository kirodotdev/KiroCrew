"""The adaptive controller's optional GPU memory probe (``adaptive/gpu_probe.py``).

Pins the rules the module docstring states: NVML is optional and a failed
import is remembered; no NVML call can raise out of ``read``; on a multi-GPU
host the most constrained device decides; every failure path yields ``-1``
fields, which the consumers treat as "not measured".
No test here needs a GPU: ``pynvml`` is a fake handed to the reader.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import types
from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest

from kiro_crew import extras, resource_status
from kiro_crew.adaptive import controller as ctl_mod
from kiro_crew.adaptive import gpu_probe
from kiro_crew.adaptive.controller import AdaptiveController, HostSample
from kiro_crew.adaptive.gpu_probe import NOT_MEASURED, GpuSample, NvmlReader
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.testing.wait import async_wait_until, await_until

MIB = 1024 * 1024

#: Lost-run ceiling on a GPU read a tick test waits out, in place of the 1 s production wait:
#: each such test took at most 0.02 s under ``-n 4`` here; a tick test's own bound is twice it.
_LOST_RUN_SECS = 30.0


@dataclass
class _Mem:
    free: int
    total: int
    used: int = 0


class _Nvml:
    """A fake ``pynvml`` module: per-device memory, an init counter."""

    def __init__(
        self, devices: list[tuple[int, int]], *, init_error: Exception | None = None
    ) -> None:
        self.devices = devices  # (free_bytes, total_bytes)
        self.init_error = init_error
        self.init_calls = 0
        self.count_error: Exception | None = None
        self.mem_errors: dict[int, Exception] = {}  # device index -> error its memory read raises

    def nvmlInit(self) -> None:
        self.init_calls += 1
        if self.init_error is not None:
            raise self.init_error

    def nvmlDeviceGetCount(self) -> int:
        if self.count_error is not None:
            raise self.count_error
        return len(self.devices)

    def nvmlDeviceGetHandleByIndex(self, index: int) -> int:
        return index

    def nvmlDeviceGetMemoryInfo(self, handle: int) -> _Mem:
        if handle in self.mem_errors:
            raise self.mem_errors[handle]
        free, total = self.devices[handle]
        return _Mem(free=free, total=total, used=total - free)


def _reader(nvml: object) -> NvmlReader:
    return NvmlReader(importer=lambda: nvml)


class TestReader:
    def test_happy_path_reports_free_memory_in_mib(self) -> None:
        nvml = _Nvml([(8 * 1024 * MIB, 32 * 1024 * MIB)])
        s = _reader(nvml).read()
        assert s == GpuSample(vram_free_mb=8192.0)
        assert s.measured

    def test_import_failure_is_remembered_and_never_retried(self) -> None:
        attempts = 0

        def importer() -> object:
            nonlocal attempts
            attempts += 1
            raise ModuleNotFoundError("No module named 'pynvml'")

        r = NvmlReader(importer=importer)
        assert r.read() == NOT_MEASURED
        assert r.read() == NOT_MEASURED
        assert attempts == 1, "a host without NVML pays one failed import per process"

    def test_init_failure_is_remembered(self) -> None:
        nvml = _Nvml([(1, 1)], init_error=RuntimeError("NVML Shared Library Not Found"))
        r = _reader(nvml)
        assert r.read() == NOT_MEASURED
        assert r.read() == NOT_MEASURED
        assert nvml.init_calls == 1

    @pytest.mark.parametrize(
        ("failure", "level"), [("import", logging.INFO), ("init", logging.WARNING)]
    )
    def test_an_unavailable_probe_says_so_once_above_debug(
        self, caplog: pytest.LogCaptureFixture, failure: str, level: int
    ) -> None:
        """The latch keeps every later read off NVML for the life of the process, so a
        GPU line set in config stays inert until a restart. The reader says so once: at
        INFO when the binding is not installed (the default host), with the command that
        installs it, at WARNING when it is installed and NVML does not start (a driver
        problem)."""
        if failure == "import":

            def importer() -> object:
                raise ModuleNotFoundError("No module named 'pynvml'")

            r = NvmlReader(importer=importer)
        else:
            nvml = _Nvml([(1, 1)], init_error=RuntimeError("NVML Shared Library Not Found"))
            r = _reader(nvml)
        with caplog.at_level(logging.DEBUG, logger=gpu_probe.__name__):
            for _ in range(3):
                assert r.read() == NOT_MEASURED
        shown = [
            rec
            for rec in caplog.records
            if rec.name == gpu_probe.__name__ and rec.levelno > logging.DEBUG
        ]
        assert [rec.levelno for rec in shown] == [level], [rec.getMessage() for rec in shown]
        text = shown[0].getMessage()
        assert "agent.vram_pressure_gb" in text and "agent.vram_critical_gb" in text, text
        if failure == "import":
            hint = extras.install_hint("gpu")
            assert hint and hint in text, f"the INFO line names no install command: {text}"
        else:
            assert "pip install" not in text, f"the binding is installed: {text}"

    def test_failure_after_init_is_not_measured_this_read_and_retried_next(self) -> None:
        nvml = _Nvml([(4 * 1024 * MIB, 8 * 1024 * MIB)])
        r = _reader(nvml)
        assert r.read().measured
        nvml.count_error = RuntimeError("GPU fell off the bus")
        assert r.read() == NOT_MEASURED
        nvml.count_error = None
        assert r.read().measured, "a transient NVML error after init is not a missing driver"

    def test_a_driver_with_no_device_is_not_measured_and_read_again_later(self) -> None:
        nvml = _Nvml([])
        r = _reader(nvml)
        assert r.read() == NOT_MEASURED
        nvml.devices.append((4 * 1024 * MIB, 8 * 1024 * MIB))
        assert r.read().measured, "no device is not a missing driver: the next read looks again"

    def test_most_constrained_device_decides(self) -> None:
        nvml = _Nvml(
            [
                (20 * 1024 * MIB, 24 * 1024 * MIB),  # roomy card
                (1 * 1024 * MIB, 32 * 1024 * MIB),  # the card a model fills
            ]
        )
        s = _reader(nvml).read()
        assert s.vram_free_mb == 1024.0, "free memory is the least free card's, not a sum"

    def test_a_card_that_reports_no_total_memory_is_not_measured(self) -> None:
        """Free 0 beside total 0 is no reading, not a full card: it must not cut the gate,
        and no other card's headroom is reported in its place."""
        assert _reader(_Nvml([(0, 0)])).read() == NOT_MEASURED
        nvml = _Nvml(
            [
                (20 * 1024 * MIB, 24 * 1024 * MIB),  # roomy card
                (0, 0),  # the least free card, with no total
            ]
        )
        s = _reader(nvml).read()
        assert s == NOT_MEASURED, f"a card with no total was read as a card: {s}"

    def test_a_device_that_fails_its_read_makes_the_read_not_measured(self) -> None:
        """The card that cannot be read may be the one a model fills, so another card's
        headroom is never reported in its place; the next read looks again."""
        nvml = _Nvml(
            [
                (20 * 1024 * MIB, 24 * 1024 * MIB),  # roomy card
                (1 * 1024 * MIB, 32 * 1024 * MIB),  # the card a model fills
            ]
        )
        r = _reader(nvml)
        nvml.mem_errors[1] = RuntimeError("GPU fell off the bus")
        s = r.read()
        assert s == NOT_MEASURED, f"an unreadable card's place was taken by another card: {s}"
        del nvml.mem_errors[1]
        assert r.read().vram_free_mb == 1024.0, "a failed device read is retried on the next read"

    def test_read_never_raises_even_when_the_fake_is_hostile(self) -> None:
        class Hostile:
            def nvmlInit(self) -> None:
                return None

            def __getattr__(self, name: str) -> object:
                raise AttributeError(name)

        assert _reader(Hostile()).read() == NOT_MEASURED


class _Manager:
    """The ``ExecActuator`` surface the controller drives, with nothing running."""

    user_max_concurrent = 4

    def __init__(self) -> None:
        self._agents: dict[str, object] = {}
        self._queue: list[object] = []
        self.running_count = 0

    def set_effective_cap(self, cap: int | None) -> int:
        return self.user_max_concurrent if cap is None else min(self.user_max_concurrent, cap)


def _controller(**agent: float) -> AdaptiveController:
    """A controller on the production probe path: no injected ``host_probe``."""
    cfg = KiroCrewConfig()
    for key, value in agent.items():
        setattr(cfg.agent, key, value)
    return AdaptiveController(_Manager(), cfg=cfg, sleep=AsyncMock())  # type: ignore[arg-type]


def _gpu_free(ctl: AdaptiveController) -> float:
    return ctl.state()["last_sample"]["vram_free_mb"]


def _fake_pynvml(nvml: _Nvml) -> types.ModuleType:
    fake = types.ModuleType("pynvml")
    for name in (
        "nvmlInit",
        "nvmlDeviceGetCount",
        "nvmlDeviceGetHandleByIndex",
        "nvmlDeviceGetMemoryInfo",
    ):
        setattr(fake, name, getattr(nvml, name))
    return fake


class TestTheTickReadsTheGpu:
    """The controller's tick reads the GPU, not ``probe_host``: only while a GPU line is
    set, on its own worker thread, one read at a time, waited for at most
    ``GPU_READ_TIMEOUT_SECS``. The controllers here take the production path (no
    injected ``host_probe``), so the real ``probe_host`` runs beside the read. A test
    that needs the read's result sets that wait to ``_LOST_RUN_SECS``, so how soon the
    executor runs the read cannot decide it."""

    def test_host_sample_defaults_say_not_measured(self) -> None:
        out = HostSample()
        assert out.vram_free_mb == -1.0

    @pytest.mark.asyncio
    async def test_with_a_line_set_the_tick_fills_the_gpu_fields_from_the_reader(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gpu_probe, "_reader", None)  # a fresh process-wide reader
        monkeypatch.setattr(ctl_mod, "GPU_READ_TIMEOUT_SECS", _LOST_RUN_SECS)
        nvml = _Nvml([(6 * 1024 * MIB, 32 * 1024 * MIB)])
        monkeypatch.setitem(sys.modules, "pynvml", _fake_pynvml(nvml))
        ctl = _controller(vram_critical_gb=1.5)
        await asyncio.wait_for(ctl.tick(), timeout=2 * _LOST_RUN_SECS)
        assert _gpu_free(ctl) == 6144.0

    @pytest.mark.asyncio
    async def test_with_a_line_set_and_no_nvml_the_gpu_fields_stay_minus_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gpu_probe, "_reader", None)  # a fresh process-wide reader
        monkeypatch.setattr(ctl_mod, "GPU_READ_TIMEOUT_SECS", _LOST_RUN_SECS)
        monkeypatch.setitem(sys.modules, "pynvml", None)  # import raises
        monkeypatch.setattr(resource_status, "_read_available_gb", lambda: 12.0)
        ctl = _controller(vram_critical_gb=1.5)
        await asyncio.wait_for(ctl.tick(), timeout=2 * _LOST_RUN_SECS)
        assert _gpu_free(ctl) == -1.0
        free = ctl.state()["last_sample"]["free_mem_mb"]
        assert free == 12288.0, "the host probe still ran"

    @pytest.mark.asyncio
    async def test_with_both_lines_off_the_tick_never_touches_nvml(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both GPU lines default to 0, so the policy has no use for the card: no read
        starts, and NVML is never imported or initialised."""
        touched: list[str] = []

        def importer() -> object:
            touched.append("import")
            raise AssertionError("NVML imported while both GPU lines are 0")

        monkeypatch.setattr(gpu_probe, "_reader", None)  # a fresh process-wide reader
        monkeypatch.setattr(gpu_probe, "_import_pynvml", importer)
        ctl = _controller()
        for _ in range(3):
            await asyncio.wait_for(ctl.tick(), timeout=2 * _LOST_RUN_SECS)
        assert touched == [], "the tick read the GPU with both lines off"
        assert _gpu_free(ctl) == -1.0

    @pytest.mark.asyncio
    async def test_a_gpu_read_that_blocks_does_not_stall_the_tick(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stuck NVML call costs the GPU fields, never the tick: host memory is still
        read and decided on. A tick that finds the read still running starts no second
        one, which would park another worker thread in the same blocked NVML call."""
        release = threading.Event()
        started = threading.Event()
        entered: list[int] = []

        def stuck_read() -> GpuSample:
            entered.append(1)
            started.set()
            # Held past the tick's bound, so a tick that waits on this read fails at that
            # bound instead of being freed by the read's own.
            release.wait(timeout=3 * _LOST_RUN_SECS)
            return GpuSample(vram_free_mb=100.0)

        monkeypatch.setattr(gpu_probe, "read_gpu", stuck_read)
        monkeypatch.setattr(resource_status, "_read_available_gb", lambda: 12.0)
        monkeypatch.setattr(ctl_mod, "GPU_READ_TIMEOUT_SECS", 0.05, raising=False)
        ctl = _controller(vram_critical_gb=1.5)
        try:
            for _ in range(2):
                try:
                    await asyncio.wait_for(ctl.tick(), timeout=2 * _LOST_RUN_SECS)
                except asyncio.TimeoutError:
                    pytest.fail("the tick waited on a GPU read that blocks")
                assert ctl.state()["last_sample"]["free_mem_mb"] == 12288.0
                assert _gpu_free(ctl) == -1.0
            # The first tick started the read, but its worker thread may not have run
            # yet. Wait for it, bounded, so the count checks the read that started
            # rather than how fast the executor scheduled its thread.
            await async_wait_until(
                started.is_set,
                timeout=_LOST_RUN_SECS,
                describe=lambda: "the stuck GPU read never ran",
            )
            assert entered == [1], f"{len(entered)} reads started while one was stuck"
        finally:
            release.set()
            pending = getattr(ctl, "_gpu_read", None)
            if pending is not None:
                await await_until(pending, what="the released GPU read")


class TestConfigKeys:
    def test_vram_thresholds_default_off_and_reach_the_policy(self) -> None:
        from kiro_crew.adaptive.policy import params_from_config
        from kiro_crew.config.sections import AgentConfig

        agent = AgentConfig()
        assert (agent.vram_pressure_gb, agent.vram_critical_gb) == (0.0, 0.0)
        cfg = types.SimpleNamespace(agent=agent)
        th = params_from_config(cfg, exec_ceiling=4).thresholds
        assert (th.vram_pressure_mb, th.vram_critical_mb) == (0.0, 0.0)

        on = types.SimpleNamespace(agent=AgentConfig(vram_pressure_gb=3.0, vram_critical_gb=1.5))
        th = params_from_config(on, exec_ceiling=4).thresholds
        assert (th.vram_pressure_mb, th.vram_critical_mb) == (3072.0, 1536.0)
