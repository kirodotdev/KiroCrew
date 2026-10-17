"""``agent.cold_start_concurrency`` and ``agent.runtime_spawn_concurrency``.

Both cold-start queues had fixed widths (4 background permits on
``SessionManager._start_sem``, 2 on the per-loop runtime spawn + initialize
admission). They are now read from config, and the default ``"auto"`` follows
the effective ``session_start_concurrency`` width (``cold_start_sizing``), never
below the old widths.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import weakref
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest

from kiro_crew import cli_doctor, cold_start_sizing, session_start_sizing
from kiro_crew.acp import runtime_start
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.schema import build_json_schema
from kiro_crew.config.sections import AgentConfig
from kiro_crew.recovery import ladder as lad
from kiro_crew.session import SessionManager
from kiro_crew.session_allocation import FOREGROUND_COLD_START_RESERVE
from kiro_crew.start_priority import StartPriority


@pytest.fixture(autouse=True)
def _isolate(_floor_monkeypatch: pytest.MonkeyPatch):
    """Fresh per-loop admissions, a fixed host width, and the doctor's ladder reset.

    Uses the floor patcher, not the test body's ``monkeypatch``, so this
    isolation has its own undo stack (a test's ``monkeypatch.undo()`` cannot
    unpin it mid-test).
    """
    _floor_monkeypatch.setattr(runtime_start, "_cold_start_admissions", weakref.WeakKeyDictionary())
    _floor_monkeypatch.setattr(cold_start_sizing, "_configured_runtime_spawn_width", None)
    lad._reset_default_ladder_for_tests()
    _pin_auto_width(_floor_monkeypatch, 2)
    yield
    lad._reset_default_ladder_for_tests()


def _pin_auto_width(monkeypatch: pytest.MonkeyPatch, width: int) -> None:
    """Fix the host-sized ``session/new`` width so no test depends on this host."""
    monkeypatch.setattr(
        session_start_sizing,
        "_host_cached",
        session_start_sizing.HostCapacity(
            affinity_cpus=width * 4, quota_cpus=None, cpus=width * 4, available_gb=1000.0
        ),
    )


def _agent(**fields: Any) -> Any:
    agent = KiroCrewConfig().agent
    for name, value in fields.items():
        setattr(agent, name, value)
    return agent


def _widths(agent: object) -> tuple[int, int]:
    """``(cold, spawn)`` through the probing path production uses (doctor, gateway)."""
    found = re.findall(r"=(\d+)", cold_start_sizing.describe_cold_start_limits(agent))
    return int(found[0]), int(found[1])


def _load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent: dict[str, Any]) -> KiroCrewConfig:
    home = tmp_path / "kc"
    home.mkdir()
    (home / "config.json").write_text(json.dumps({"agent": agent}))
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    return KiroCrewConfig.load()


def _manager(cfg: KiroCrewConfig) -> SessionManager:
    return SessionManager(cfg, provider_factory=lambda *a, **k: object())


def _background_width(mgr: SessionManager) -> int:
    return mgr._start_sem._value - FOREGROUND_COLD_START_RESERVE


# --------------------------------------------------------------------------- #
# Config surface
# --------------------------------------------------------------------------- #


def test_defaults_are_auto() -> None:
    agent = KiroCrewConfig().agent
    assert agent.cold_start_concurrency == "auto"
    assert agent.runtime_spawn_concurrency == "auto"


def test_loader_defaults_to_auto(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent = _load(tmp_path, monkeypatch, {}).agent
    assert agent.cold_start_concurrency == "auto"
    assert agent.runtime_spawn_concurrency == "auto"


@pytest.mark.parametrize(
    ("written", "loaded"),
    [
        (16, 16),
        (1, 1),
        (32, 32),
        (0, 1),
        (500, 32),
        ("8", 8),
        (8.0, 8),
        ("AUTO", "auto"),
        (True, "auto"),
        ("nope", "auto"),
        (2.5, "auto"),
        (None, "auto"),
    ],
)
def test_loader_reads_and_clamps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, written: Any, loaded: Any
) -> None:
    agent = _load(
        tmp_path,
        monkeypatch,
        {"cold_start_concurrency": written, "runtime_spawn_concurrency": written},
    ).agent
    assert agent.cold_start_concurrency == loaded
    assert agent.runtime_spawn_concurrency == loaded


def test_schema_accepts_an_integer_and_auto() -> None:
    props = build_json_schema(AgentConfig)["properties"]
    for name in ("cold_start_concurrency", "runtime_spawn_concurrency"):
        assert props[name]["type"] == ["integer", "string"]


# --------------------------------------------------------------------------- #
# Sizing
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("width", "cold", "spawn"),
    [
        (1, 4, 2),  # explicit width 1: never below the old widths
        (2, 4, 2),  # laptop, or a host under a small cpu.max quota
        (4, 4, 2),  # 16 vCPU
        (8, 8, 4),  # 32 vCPU
        (10, 10, 5),  # 64 vCPU with ~30 GB free: memory bounds the width
        (16, 16, 8),  # 64+ vCPU: the measured 16 / 8
        (64, 16, 8),  # an explicit wide gate: auto stays at what was measured
    ],
)
def test_auto_limits_follow_the_start_width(width: int, cold: int, spawn: int) -> None:
    assert cold_start_sizing.auto_cold_start_limits(width) == (cold, spawn)


@pytest.mark.parametrize(("width", "cold", "spawn"), [(2, 4, 2), (4, 4, 2), (8, 8, 4), (16, 16, 8)])
def test_auto_reads_the_host_sized_start_width(
    monkeypatch: pytest.MonkeyPatch, width: int, cold: int, spawn: int
) -> None:
    _pin_auto_width(monkeypatch, width)
    agent = _agent(session_start_concurrency="auto")
    assert _widths(agent) == (cold, spawn)


def test_auto_follows_an_explicit_start_width(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pinned ``session/new`` width moves the cold-start queues with it."""
    _pin_auto_width(monkeypatch, 16)
    agent = _agent(session_start_concurrency=2)
    assert _widths(agent) == (4, 2)


def test_an_explicit_integer_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    _pin_auto_width(monkeypatch, 16)
    agent = _agent(cold_start_concurrency=3, runtime_spawn_concurrency=1)
    assert _widths(agent) == (3, 1)


@pytest.mark.parametrize("stray", [object(), mock.MagicMock(), True, 2.0, None])
def test_a_stray_value_falls_back_to_the_old_widths(stray: Any) -> None:
    """A hand-built or mocked config must not size a queue (``int(MagicMock())`` is 1)."""
    agent = _agent(cold_start_concurrency=stray, runtime_spawn_concurrency=stray)
    assert _widths(agent) == (4, 2)
    assert _widths(mock.MagicMock()) == (4, 2)
    assert _widths(None) == (4, 2)


def test_an_unreadable_start_width_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_configured: object) -> int:
        raise OSError("cgroup unreadable")

    monkeypatch.setattr(cold_start_sizing, "effective_session_start_concurrency", boom)
    agent = _agent()
    assert _widths(agent) == (4, 2)


def _record_host_probe_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> list[bool]:
    on_main_thread: list[bool] = []

    def probe() -> session_start_sizing.HostCapacity:
        on_main_thread.append(threading.current_thread() is threading.main_thread())
        return session_start_sizing.HostCapacity(
            affinity_cpus=8, quota_cpus=None, cpus=8, available_gb=1000.0
        )

    monkeypatch.setattr(session_start_sizing, "_host_cached", None)
    monkeypatch.setattr(session_start_sizing, "probe_host_capacity", probe)
    return on_main_thread


@pytest.mark.asyncio
async def test_gateway_boot_sizes_every_width_in_one_thread_hop(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The post-bind sizing task reads the host in one thread hop, logs every
    width, then widens the session manager's queues on the loop."""
    import logging

    from kiro_crew.slack.gateway import GatewayOrchestrator

    on_main_thread = _record_host_probe_thread(monkeypatch)
    hops: list[object] = []
    real_to_thread = asyncio.to_thread

    async def counting_to_thread(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        hops.append(func)
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", counting_to_thread)
    widened_on: list[bool] = []
    sessions = SimpleNamespace(
        widen_cold_start_queues=lambda: widened_on.append(
            threading.current_thread() is threading.main_thread()
        )
    )
    orch = SimpleNamespace(_cfg=KiroCrewConfig(), sessions=sessions)
    with caplog.at_level(logging.INFO, logger="kiro_crew.slack.gateway"):
        await GatewayOrchestrator._log_session_start_sizing(orch)  # type: ignore[arg-type]

    assert len(hops) == 1
    assert on_main_thread == [False]
    assert widened_on == [True]  # on the loop, after the reading exists
    messages = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("Session start concurrency: ") for m in messages)
    assert any(m.startswith("Cold-start concurrency: cold_start_concurrency=") for m in messages)


def test_gateway_boot_path_takes_no_sizing_step() -> None:
    """Nothing before the socket binds sizes a queue (``no-new-work-on-gateway-boot-path``).

    ``_init_services`` builds the manager at the cached widths without awaiting
    any sizing; the host is read by the post-bind task ``run()`` schedules, and
    that task is what widens the queues.
    """
    import inspect

    from kiro_crew.slack.gateway import GatewayOrchestrator

    init_services = inspect.getsource(GatewayOrchestrator._init_services)
    run = inspect.getsource(GatewayOrchestrator.run)
    sizing = inspect.getsource(GatewayOrchestrator._log_session_start_sizing)
    assert "_log_session_start_sizing" not in init_services
    assert "describe_cold_start_limits" not in init_services
    assert "await self._log_session_start_sizing()" not in run
    assert "self._schedule_session_start_sizing()" in run
    assert "widen_cold_start_queues()" in sizing


# --------------------------------------------------------------------------- #
# SessionManager._start_sem
# --------------------------------------------------------------------------- #


def test_session_manager_sizes_its_start_semaphore_from_config() -> None:
    cfg = KiroCrewConfig()
    cfg.agent.cold_start_concurrency = 9
    mgr = _manager(cfg)
    assert _background_width(mgr) == 9
    assert mgr._start_sem._background_cap == 9


@pytest.mark.parametrize(("width", "cold"), [(2, 4), (8, 8), (16, 16)])
def test_session_manager_auto_width(monkeypatch: pytest.MonkeyPatch, width: int, cold: int) -> None:
    _pin_auto_width(monkeypatch, width)
    cfg = KiroCrewConfig()
    cfg.agent.session_start_concurrency = "auto"  # type: ignore[assignment]
    assert _background_width(_manager(cfg)) == cold


def test_session_manager_never_probes_the_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Built on the boot path and the loop: without a host reading, "auto" is the floor."""
    monkeypatch.setattr(session_start_sizing, "_host_cached", None)

    probes: list[int] = []

    def probe() -> session_start_sizing.HostCapacity:
        probes.append(1)
        return session_start_sizing.HostCapacity(
            affinity_cpus=64, quota_cpus=None, cpus=64, available_gb=1000.0
        )

    monkeypatch.setattr(session_start_sizing, "probe_host_capacity", probe)
    mgr = _manager(KiroCrewConfig())
    assert probes == []
    assert _background_width(mgr) == cold_start_sizing.COLD_START_DEFAULT
    assert (
        cold_start_sizing.configured_runtime_spawn_width()
        == cold_start_sizing.RUNTIME_SPAWN_DEFAULT
    )


@pytest.mark.asyncio
async def test_widening_moves_both_queues_to_the_host_sized_widths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gateway's post-bind reading grows a manager built at the floors."""
    monkeypatch.setattr(session_start_sizing, "_host_cached", None)
    mgr = _manager(KiroCrewConfig())
    admission = runtime_start._cold_start_admission()
    assert admission.semaphore.limit == 2
    _pin_auto_width(monkeypatch, 16)  # the reading lands

    mgr.widen_cold_start_queues()

    assert _background_width(mgr) == 16
    assert mgr._start_sem._background_cap == 16
    assert mgr._start_sem.limit == 16 + FOREGROUND_COLD_START_RESERVE
    assert cold_start_sizing.configured_runtime_spawn_width() == 8
    assert runtime_start._cold_start_admission() is admission
    assert admission.semaphore.limit == 8


def test_widening_leaves_an_explicit_width_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = KiroCrewConfig()
    cfg.agent.cold_start_concurrency = 3
    cfg.agent.runtime_spawn_concurrency = 1
    mgr = _manager(cfg)
    _pin_auto_width(monkeypatch, 16)
    mgr.widen_cold_start_queues()
    assert _background_width(mgr) == 3
    assert cold_start_sizing.configured_runtime_spawn_width() == 1


@pytest.mark.asyncio
async def test_the_identity_sweep_drain_waits_for_every_configured_permit() -> None:
    """The sweep's barrier is the semaphore's own capacity, so it covers a wider queue."""
    cfg = KiroCrewConfig()
    cfg.agent.cold_start_concurrency = 7
    sem = _manager(cfg)._start_sem
    for _ in range(7):
        await asyncio.wait_for(sem.acquire(StartPriority.BACKGROUND), 5)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def sweep() -> None:
        async with sem.drain():
            entered.set()
            await release.wait()

    task = asyncio.create_task(sweep())
    for _ in range(6):
        sem.release(StartPriority.BACKGROUND)
    await asyncio.sleep(0)
    assert not entered.is_set()  # one of the 7 starts is still inside
    sem.release(StartPriority.BACKGROUND)
    await asyncio.wait_for(entered.wait(), 5)
    assert sem._drain_held == 7 + FOREGROUND_COLD_START_RESERVE
    release.set()
    await asyncio.wait_for(task, 5)


# --------------------------------------------------------------------------- #
# Runtime spawn + initialize admission
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_runtime_admission_is_sized_from_config() -> None:
    cfg = KiroCrewConfig()
    cfg.agent.runtime_spawn_concurrency = 6
    _manager(cfg)
    assert cold_start_sizing.configured_runtime_spawn_width() == 6
    assert runtime_start._cold_start_admission().semaphore._value == 6


@pytest.mark.asyncio
async def test_runtime_admission_auto_follows_the_start_width(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pin_auto_width(monkeypatch, 16)
    cfg = KiroCrewConfig()
    cfg.agent.session_start_concurrency = "auto"  # type: ignore[assignment]
    _manager(cfg)
    assert runtime_start._cold_start_admission().semaphore._value == 8


@pytest.mark.asyncio
async def test_without_a_session_manager_the_admission_keeps_the_old_width() -> None:
    """An embedder or test that spawns runtimes directly gets the historical 2."""
    assert cold_start_sizing.configured_runtime_spawn_width() is None
    admission = runtime_start._cold_start_admission()
    assert admission.semaphore._value == runtime_start._COLD_START_MAX_CONCURRENT


@pytest.mark.asyncio
async def test_an_admission_made_before_the_manager_grows_to_its_width() -> None:
    """A spawn that ran before any manager published is not stuck at the default."""
    first = runtime_start._cold_start_admission()
    assert first.semaphore.limit == runtime_start._COLD_START_MAX_CONCURRENT
    cfg = KiroCrewConfig()
    cfg.agent.runtime_spawn_concurrency = 7
    _manager(cfg)
    assert runtime_start._cold_start_admission() is first
    assert first.semaphore.limit == 7
    assert first.semaphore._value == 7


@pytest.mark.asyncio
async def test_an_admission_never_shrinks() -> None:
    cfg = KiroCrewConfig()
    cfg.agent.runtime_spawn_concurrency = 6
    _manager(cfg)
    first = runtime_start._cold_start_admission()
    cfg.agent.runtime_spawn_concurrency = 1
    _manager(cfg)
    assert runtime_start._cold_start_admission() is first
    assert first.semaphore.limit == 6


def test_gateway_builds_the_session_manager_before_anything_can_spawn() -> None:
    """The published width only takes effect if no runtime spawns first.

    ``run()`` calls ``_init_services`` before any other ``_init_*`` step (MCP
    gateway, cron, subagents, task runner, dashboard, warm pools), and inside
    ``_init_services`` nothing before ``SessionManager(`` creates a runtime.
    """
    import inspect
    import re

    from kiro_crew.slack.gateway import GatewayOrchestrator

    run = inspect.getsource(GatewayOrchestrator.run)
    init_steps = re.findall(r"self\.(_init_\w+)\(", run)
    assert init_steps and init_steps[0] == "_init_services"
    init_services = inspect.getsource(GatewayOrchestrator._init_services)
    before_manager = init_services[: init_services.index("SessionManager(")]
    for spawner in ("AcpRuntime", ".spawn(", "warm_pool", "WarmPool", "_cold_start_admission"):
        assert spawner not in before_manager, spawner


@pytest.mark.asyncio
async def test_spawn_enters_an_admission_of_the_configured_width(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``spawn`` takes no extra await before admission, so concurrent spawns queue
    in arrival order (a pre-admission await let a third spawn trail the check)."""
    from kiro_crew.acp.runtime import AcpRuntime

    cfg = KiroCrewConfig()
    cfg.agent.runtime_spawn_concurrency = 5
    _manager(cfg)
    seen: list[int] = []

    async def _spawned(self: AcpRuntime) -> None:
        seen.append(runtime_start._cold_start_admission().semaphore._limit)

    monkeypatch.setattr(AcpRuntime, "_spawn_admitted_rederiving_once", _spawned)
    await asyncio.wait_for(AcpRuntime().spawn(), 5)
    assert seen == [5]


# --------------------------------------------------------------------------- #
# kirocrew doctor
# --------------------------------------------------------------------------- #


def test_doctor_prints_explicit_limits(capsys: pytest.CaptureFixture[str]) -> None:
    cfg = KiroCrewConfig()
    cfg.agent.cold_start_concurrency = 12
    cfg.agent.runtime_spawn_concurrency = 5
    cli_doctor._doctor_overload_resilience(cfg)
    out = capsys.readouterr().out
    assert "cold_start_concurrency=12 runtime_spawn_concurrency=5 " in out


def test_doctor_prints_resolved_auto_limits(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _pin_auto_width(monkeypatch, 16)
    cfg = KiroCrewConfig()
    cfg.agent.session_start_concurrency = "auto"  # type: ignore[assignment]
    cli_doctor._doctor_overload_resilience(cfg)
    out = capsys.readouterr().out
    assert "cold_start_concurrency=16 (auto) runtime_spawn_concurrency=8 (auto) " in out
