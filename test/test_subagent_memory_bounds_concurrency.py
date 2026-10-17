"""Memory, not a count cap, bounds how many subagents run at once.

The subagent count is a high ceiling (``agent.subagent_auto_max``, or an explicit
``agent.max_subagents``) standing in for provider concurrency and fd / PID
limits; free memory is bounded per start by the spawn floor
(``agent.spawn_min_memory_gb``). Before this, three count bounds sat under that
floor and throttled one chat's subagents behind another's while memory was
still free: the auto cap was sized from host memory, the adaptive execution cap
started at 4 and halved on 250 ms of gateway loop lag or low memory alone, and
the MCP gateway spawn gate topped out at 8 backend initializations.

Pinned here, one test per acceptance criterion:

* (i) ``max_subagents=0`` with 20 spawns on a host simulated at 40 GiB: all 20
  start, paced only by the stagger;
* (ii) 3 s of injected loop lag -- or memory at the critical line -- leaves the
  execution cap unchanged (the spawn gate still backs off);
* (iii) an explicit ``max_subagents=3`` still caps, and the child reserve still
  lets a waiting parent's child start;
* (iv) the spawn gate can carry one backend initialization per subagent the cap
  admits, and the host budget admits that fan-out;
* (v) the timeout / slow-start / fd / process back-off still cuts the execution
  cap, and a provider 429 still never does;
* (vi) the prompt figure and the spawn tool description are a defined number;
* (vii) the TaskRunner auto value is non-zero.
"""

from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from overload_fakes import Clock, ManagerHarness, mock_ctx, mock_sessions, wait_taskq_open

import kiro_crew.subagent as subagent_mod
from kiro_crew.adaptive.controller import AdaptiveController, HostSample
from kiro_crew.adaptive.policy import AdaptivePolicy, params_from_config
from kiro_crew.adaptive.signals import (
    SIGNAL_FDS,
    SIGNAL_LOOP_LAG,
    SIGNAL_MEMORY,
    SIGNAL_PROCS,
    Sample,
    SpawnGateStats,
)
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.subagent import (
    SubagentInfo,
    SubagentManager,
    resolve_max_subagents,
    subagent_count_ceiling,
)
from kiro_crew.subagent_manager.admission import FairnessSettings, SpawnAdmissionCoordinator
from kiro_crew.taskq import model

pytestmark = [pytest.mark.timeout(60), pytest.mark.usefixtures("healthy_host_memory")]


@pytest.fixture
def quiet(monkeypatch):
    monkeypatch.setattr(subagent_mod, "Stats", MagicMock())
    monkeypatch.setattr(subagent_mod, "sel", MagicMock())


@pytest.fixture
def host_gb(monkeypatch):
    """Pin the host memory reading every sizing path consults to *gb*."""

    def _apply(gb: float) -> None:
        monkeypatch.setattr(subagent_mod, "_available_memory_gb", lambda: gb)
        monkeypatch.setattr(subagent_mod, "read_cap_costs", lambda *a, **k: (None, None))

    return _apply


class _FakeManager:
    """The controller's view of a SubagentManager: ceiling in, effective cap out."""

    def __init__(self, ceiling: int) -> None:
        self.user_max_concurrent = ceiling
        self.effective: int | None = None
        self.running_count = 0
        self._agents: dict = {}

    def set_effective_cap(self, cap):
        self.effective = cap
        return cap if cap is not None else self.user_max_concurrent


# ── (i) 20 auto-sized spawns all start on a 40 GiB host ─────────────────────


@pytest.mark.asyncio
async def test_twenty_auto_sized_spawns_all_start_on_a_40_gib_host(
    monkeypatch, quiet, host_gb
) -> None:
    cfg = KiroCrewConfig()
    assert cfg.agent.max_subagents == 0, "auto is the shipped default"
    cfg.agent.subagent_spawn_stagger_secs = 0.25
    monkeypatch.setattr(KiroCrewConfig, "load", lambda: cfg)
    monkeypatch.setattr(SpawnAdmissionCoordinator, "open_store_off_loop", True)
    monkeypatch.setattr(SpawnAdmissionCoordinator, "pump_off_loop", True)
    host_gb(40.0)
    cap = resolve_max_subagents(cfg)
    assert cap == cfg.agent.subagent_auto_max >= 20

    clock = Clock()
    monkeypatch.setattr(subagent_mod, "time", SimpleNamespace(monotonic=clock, time=time.time))
    mgr = SubagentManager(sessions=mock_sessions(), ctx_builder=mock_ctx(), max_concurrent=cap)
    await wait_taskq_open(mgr)
    mgr._spawn_stagger_secs = cfg.agent.subagent_spawn_stagger_secs
    starts: dict[str, float] = {}
    refused: list[float] = []
    timer_handles = []
    loop = asyncio.get_running_loop()
    real_call_later = loop.call_later

    def call_later(delay, callback, *args, **kwargs):
        # The pump's stagger timer runs on virtual time (the test advances it);
        # asyncio's own deadlines keep the real clock and stay bounded.
        if callback == mgr._drain_queue:
            handle = real_call_later(3600, callback, *args, **kwargs)
            timer_handles.append(handle)
            return handle
        return real_call_later(delay, callback, *args, **kwargs)

    monkeypatch.setattr(loop, "call_later", call_later)

    def available() -> float:
        # Each live worker holds 0.05 GiB while warming and 0.5 GiB once settled.
        resident = sum(
            0.5 if clock() - started >= 5.0 else 0.05
            for agent_id, started in starts.items()
            if not mgr._agents[agent_id].done
        )
        return 40.0 - resident

    def memory_check(*, min_gb, **_kw):
        free = available()
        if free < min_gb:
            refused.append(clock())
        return free >= min_gb, free

    monkeypatch.setattr(subagent_mod, "check_memory_available", memory_check)
    finish = asyncio.Event()

    async def worker(info: SubagentInfo) -> None:
        starts[info.id] = clock()
        info._pid = 1000 + len(starts)
        info._exec_started = time.time()
        info._session_sharing = False
        await finish.wait()
        info.done = True
        info.result = "ok"
        mgr._claim_finalize(info)
        if mgr._release_slot(info):
            mgr._running_count -= 1

    monkeypatch.setattr(mgr, "_run", worker)
    # The real controller is attached: its fresh-start execution cap is what a
    # gateway applies before the first spawn.
    ctl = AdaptiveController(
        mgr,
        cfg=cfg,
        clock=clock,
        host_probe=lambda: HostSample(free_mem_mb=available() * 1024),
    )

    async def pump() -> None:
        mgr._drain_queue()
        task = getattr(mgr, "_drain_task", None)
        if task is not None:
            await asyncio.wait_for(asyncio.shield(task), 5)
        await asyncio.sleep(0)

    try:
        await ctl.tick()
        assert mgr.max_concurrent == cap, "the execution cap starts at the ceiling"
        for i in range(20):
            await mgr.spawn_async(
                f"work-{i}", parent_session_key="dash:wide", batch_id="wide", batch_total=20
            )
        await pump()
        for step in range(1, 41):
            clock.advance(0.25)
            await pump()
            if step % 20 == 0:
                await ctl.tick()
        assert len(starts) == 20, (len(starts), mgr.max_concurrent)
        assert not refused, "40 GiB holds 20 starts: memory never refused one"
        # Paced by the stagger alone: one start per interval, never a burst.
        launched = sorted(starts.values())
        assert all(b - a >= 0.25 for a, b in zip(launched, launched[1:]))
        assert launched[-1] - launched[0] <= 20 * 0.25
        assert mgr.max_concurrent == cap
    finally:
        finish.set()
        mgr._shutting_down = True
        for handle in timer_handles:
            handle.cancel()
        tasks = [task for task in mgr._tasks.values() if not task.done()]
        drain = getattr(mgr, "_drain_task", None)
        if drain is not None and not drain.done():
            tasks.append(drain)
        for task in tasks:
            task.cancel()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)
        mgr._taskq.close()


def test_the_auto_ceiling_is_not_sized_from_host_memory(host_gb) -> None:
    cfg = KiroCrewConfig()
    for gb in (6.0, 16.0, 40.0, 200.0):
        host_gb(gb)
        assert resolve_max_subagents(cfg) == cfg.agent.subagent_auto_max
    # Memory that cannot be read leaves the floor unable to bound starts, so
    # the count falls back to the legacy 3.
    host_gb(-1.0)
    assert resolve_max_subagents(cfg) == 3


# ── (ii) loop lag and memory do not move the execution cap ──────────────────


def test_three_seconds_of_loop_lag_leaves_the_subagent_cap_unchanged() -> None:
    pol = AdaptivePolicy(params_from_config(KiroCrewConfig(), exec_ceiling=32))
    assert pol.exec_cap == 32
    seen_signals: set[str] = set()
    for i in range(12):
        d = pol.observe(
            Sample(t=i * 5.0, loop_lag_ms=3000.0, free_mem_mb=16_000.0, running=20, queued=5)
        )
        seen_signals.update(d.signals)
        assert d.effective_exec_cap == 32, d
    assert SIGNAL_LOOP_LAG in seen_signals
    # The gateway's loop lag is no evidence about the MCP daemon's forks
    # either: the spawn gate holds at its fresh start, unpaused.
    assert pol.gate_cap == pol.params.gate_start and not pol.paused


def test_memory_at_the_critical_line_leaves_the_subagent_cap_unchanged() -> None:
    pol = AdaptivePolicy(params_from_config(KiroCrewConfig(), exec_ceiling=32))
    for i in range(6):
        d = pol.observe(Sample(t=i * 5.0, free_mem_mb=1024.0, running=8, queued=8))
        assert SIGNAL_MEMORY in d.signals
        assert d.effective_exec_cap == 32, d
    assert pol.snapshot()["last_cut"] is None


@pytest.mark.asyncio
async def test_injected_loop_lag_through_the_controller_leaves_the_cap_unchanged() -> None:
    clock = Clock()
    mgr = _FakeManager(ceiling=32)
    ctl = AdaptiveController(
        mgr,
        cfg=KiroCrewConfig(),
        clock=clock,
        host_probe=lambda: HostSample(free_mem_mb=16_000.0),
    )
    assert mgr.effective == 32
    for _ in range(10):
        clock.advance(5.0)
        d = await ctl.tick(loop_lag_ms=3000.0)
        assert d.effective_exec_cap == 32
    assert mgr.effective == 32


# ── (iii) an explicit N still caps; the child reserve still prevents deadlock ─


@pytest.mark.asyncio
async def test_an_explicit_three_still_caps_and_a_waiting_parents_child_still_starts(
    quiet,
) -> None:
    cap = resolve_max_subagents(SimpleNamespace(agent=SimpleNamespace(max_subagents=3)))
    assert cap == 3
    hz = ManagerHarness(cap, FairnessSettings(child_reserve=1))
    try:
        await hz.mgr.wait_taskq_ready()
        ctl = AdaptiveController(
            hz.mgr, cfg=KiroCrewConfig(), host_probe=lambda: HostSample(free_mem_mb=16_000.0)
        )
        assert hz.mgr.max_concurrent == 3, "the controller starts at the explicit ceiling"
        roots = [hz.spawn(f"R{i}", parent=f"dash:{i}") for i in range(5)]
        await hz.settle()
        await ctl.tick()
        states = [hz.state(r) for r in roots]
        assert states.count(model.RUNNING) == 3 and states.count(model.QUEUED) == 2
        # A running parent blocks on a child: it yields its slot, and the
        # reserve hands the slot to the child, never to a queued root.
        parent = roots[0]
        hz.block_in_spawn_sub_agents(parent)
        child = hz.child_of(parent, "C")
        await hz.settle()
        assert hz.state(child) == model.RUNNING
        assert [hz.state(r) for r in roots[3:]] == [model.QUEUED, model.QUEUED]
        assert hz.mgr._running_count == 3
    finally:
        hz.close()


# ── (iv) the spawn gate and the host budget carry the fan-out ───────────────


@pytest.fixture
def no_memory_read(monkeypatch):
    """Fail any host memory read: the boot-path ceiling is config alone."""

    def _refuse() -> float:
        raise AssertionError("the boot-path ceiling must not read host memory")

    monkeypatch.setattr(subagent_mod, "_available_memory_gb", _refuse)


@pytest.mark.parametrize(("pin", "expected"), [(50, 50), (2, 3), (1, 3)])
def test_the_count_ceiling_keeps_an_explicit_pin_floored_at_three(
    no_memory_read, pin: int, expected: int
) -> None:
    cfg = KiroCrewConfig()
    cfg.agent.max_subagents = pin
    # A pin above the auto ceiling is the operator's choice, not clamped to it.
    cfg.agent.subagent_auto_max = 32
    assert subagent_count_ceiling(cfg) == expected


def test_the_count_ceiling_is_the_auto_ceiling_when_unpinned(no_memory_read) -> None:
    cfg = KiroCrewConfig()
    assert cfg.agent.max_subagents == 0
    assert subagent_count_ceiling(cfg) == cfg.agent.subagent_auto_max


def test_the_spawn_gate_ceiling_reaches_the_subagent_ceiling() -> None:
    from kiro_crew.mcp_gateway.admission import derive_spawn_gate_ceiling

    cfg = KiroCrewConfig()
    configured = cfg.mcp_gateway.spawn_concurrency_max
    ceiling = derive_spawn_gate_ceiling(configured, cfg.agent.subagent_auto_max)
    assert ceiling == cfg.agent.subagent_auto_max >= 20
    # An operator ceiling above the subagent ceiling is kept; an explicit small
    # max_subagents never lowers the configured gate ceiling.
    assert derive_spawn_gate_ceiling(64, 32) == 64
    assert derive_spawn_gate_ceiling(configured, 3) == configured


@pytest.mark.asyncio
async def test_twenty_backend_initializations_run_at_once_under_the_derived_ceiling() -> None:
    from kiro_crew.mcp_gateway.admission import SpawnGate, derive_spawn_gate_ceiling

    cfg = KiroCrewConfig()
    gate = SpawnGate(
        cfg.mcp_gateway.spawn_concurrency_initial,
        floor=cfg.mcp_gateway.spawn_concurrency_min,
        ceiling=derive_spawn_gate_ceiling(
            cfg.mcp_gateway.spawn_concurrency_max, cfg.agent.subagent_auto_max
        ),
    )
    assert gate.set_capacity(20) == 20, "the daemon's clamp does not hold the gate at 8"
    permits = await asyncio.wait_for(
        asyncio.gather(*(gate.acquire(label=f"b{i}") for i in range(20))), 5
    )
    assert gate.in_flight == 20 and gate.queued == 0
    for permit in permits:
        permit.release()


def test_the_gate_track_climbs_past_eight_on_clean_init_evidence() -> None:
    from kiro_crew.mcp_gateway.admission import derive_spawn_gate_ceiling

    cfg = KiroCrewConfig()
    pol = AdaptivePolicy(
        params_from_config(
            cfg,
            exec_ceiling=cfg.agent.subagent_auto_max,
            gate_ceiling=derive_spawn_gate_ceiling(
                cfg.mcp_gateway.spawn_concurrency_max, cfg.agent.subagent_auto_max
            ),
        )
    )
    successes = 0
    t = 0.0
    for _ in range(40):
        successes += 20
        cap = pol.gate_cap
        busy = SpawnGateStats(capacity=cap, in_flight=cap, queued=8, successes=successes)
        pol.observe(Sample(t=t, loop_lag_ms=10.0, free_mem_mb=40_000.0, spawn_gate=busy))
        t += 5.0
    assert pol.gate_cap >= 20


@pytest.mark.asyncio
async def test_the_daemon_is_launched_with_the_subagent_ceiling_at_boot(tmp_path, host_gb) -> None:
    """The broker starts BEFORE ``_init_subagents`` builds the manager.

    So the daemon's ceiling must come from config: a figure read off the manager
    is 0 on the boot path, the daemon launched at the plain 8, and it clamped
    every raise the controller's gate track (given 32) asked for.
    """
    from kiro_crew.adaptive import controller as adaptive_controller
    from kiro_crew.slack import gateway as gw

    host_gb(40.0)
    cfg = KiroCrewConfig()
    cfg.mcp_gateway.enabled = True
    cfg.mcp_gateway.stub_servers = ["alpha-mcp"]
    with patch.object(cfg, "load_credentials", return_value={"KIROCREW_OWNER_ID": "U_OWNER"}):
        orch = gw.GatewayOrchestrator(cfg, no_dashboard=True, no_crons=True, no_open=True)
    assert orch.subagent_mgr is None, "boot order: the manager does not exist yet"

    manager = MagicMock()
    manager.start = AsyncMock(return_value=False)
    with (
        patch("kiro_crew.slack.gateway.is_gateway_supported", return_value=True),
        patch(
            "kiro_crew.slack.gateway.rewrite_kwargs",
            side_effect=lambda _cfg, stubs: {
                "socket_path": tmp_path / "gw.sock",
                "stub_servers": stubs,
            },
        ),
        patch("kiro_crew.slack.gateway.rewrite_agents", return_value=(None, {})),
        patch("kiro_crew.slack.gateway.GatewayManager", return_value=manager) as mgr_cls,
    ):
        await orch._init_mcp_gateway()
    prefetch = orch._mcp_resolve_prefetch
    prefetch.cancel()
    await asyncio.gather(prefetch, return_exceptions=True)

    spec = mgr_cls.call_args.args[0]
    assert spec.spawn_concurrency_max == cfg.agent.subagent_auto_max >= 20

    # The controller, started later beside the real manager, targets the same
    # ceiling the daemon clamps to.
    orch.subagent_mgr = _FakeManager(resolve_max_subagents(cfg))
    orch._adaptive_controller = None
    factory = MagicMock()
    with (
        patch.object(adaptive_controller, "AdaptiveController", factory),
        patch.object(adaptive_controller, "register", MagicMock()),
        patch.object(type(orch), "_wire_overload_health", MagicMock()),
    ):
        orch._start_adaptive_controller()
    assert factory.call_args.kwargs["gate_ceiling"] == spec.spawn_concurrency_max


@pytest.mark.asyncio
async def test_the_boot_path_sizes_the_gate_without_probing_host_memory(tmp_path) -> None:
    """``_init_mcp_gateway`` runs before readiness, so its ceiling is a config read.

    Reading host memory there (``/proc/meminfo`` and the cgroup ancestry, or a
    Mach call on macOS) is new synchronous work on the boot path; the gate needs
    only the upper bound the cap can reach.
    """
    from kiro_crew import subagent as subagent_mod
    from kiro_crew.adaptive import controller as adaptive_controller
    from kiro_crew.slack import gateway as gw

    probe = MagicMock(return_value=40.0)
    cfg = KiroCrewConfig()
    cfg.mcp_gateway.enabled = True
    cfg.mcp_gateway.stub_servers = ["alpha-mcp"]
    with patch.object(cfg, "load_credentials", return_value={"KIROCREW_OWNER_ID": "U_OWNER"}):
        orch = gw.GatewayOrchestrator(cfg, no_dashboard=True, no_crons=True, no_open=True)

    manager = MagicMock()
    manager.start = AsyncMock(return_value=False)
    with (
        patch.object(subagent_mod, "_available_memory_gb", probe),
        patch("kiro_crew.slack.gateway.is_gateway_supported", return_value=True),
        patch(
            "kiro_crew.slack.gateway.rewrite_kwargs",
            side_effect=lambda _cfg, stubs: {
                "socket_path": tmp_path / "gw.sock",
                "stub_servers": stubs,
            },
        ),
        patch("kiro_crew.slack.gateway.rewrite_agents", return_value=(None, {})),
        patch("kiro_crew.slack.gateway.GatewayManager", return_value=manager) as mgr_cls,
    ):
        await orch._init_mcp_gateway()
        prefetch = orch._mcp_resolve_prefetch
        prefetch.cancel()
        await asyncio.gather(prefetch, return_exceptions=True)

        orch.subagent_mgr = _FakeManager(cfg.agent.subagent_auto_max)
        orch._adaptive_controller = None
        factory = MagicMock()
        with (
            patch.object(adaptive_controller, "AdaptiveController", factory),
            patch.object(adaptive_controller, "register", MagicMock()),
            patch.object(type(orch), "_wire_overload_health", MagicMock()),
        ):
            orch._start_adaptive_controller()

    probe.assert_not_called()
    assert mgr_cls.call_args.args[0].spawn_concurrency_max == cfg.agent.subagent_auto_max
    assert factory.call_args.kwargs["gate_ceiling"] == cfg.agent.subagent_auto_max


def test_the_inert_adaptive_initial_does_not_warn_on_every_launch(caplog) -> None:
    """Every save writes ``agent.adaptive_initial``, so a deprecation flag on it
    would announce itself on every load of every config."""
    from kiro_crew.config import validation

    if not validation._HAS_JSONSCHEMA:
        pytest.skip("jsonschema not installed")
    data = KiroCrewConfig().to_dict()
    assert data["agent"]["adaptive_initial"] == 4
    with caplog.at_level(logging.WARNING):
        validation.validate_config_data(data)
    assert not any("agent.adaptive_initial" in r.getMessage() for r in caplog.records)


def test_the_host_budget_admits_twenty_subagents_of_backends_at_40_gib(monkeypatch) -> None:
    from kiro_crew.mcp_gateway import host_budget

    monkeypatch.setattr(host_budget, "_nofile_soft_limit", lambda: 1024)
    gw = KiroCrewConfig().mcp_gateway
    limits = host_budget.resolve_limits(
        max_procs=gw.host_budget_max_procs,
        max_rss_mb=gw.host_budget_max_rss_mb,
        max_fds=gw.host_budget_max_fds,
        available_mb=40 * 1024.0,
        max_backends=gw.max_backends,
    )
    budget = host_budget.HostBudget(limits)
    # Twenty subagents with five private MCP servers each.
    charges = [budget.reserve(label=f"b{i}", kind="exclusive") for i in range(100)]
    assert budget.procs_in_use == 100
    for charge in charges:
        charge.release()


# ── (v) the work-evidence back-off still cuts the execution cap ─────────────


def _clean(t: float, **over: object) -> Sample:
    base: dict[str, object] = dict(t=t, loop_lag_ms=10.0, free_mem_mb=16_000.0)
    base.update(over)
    return Sample(**base)  # type: ignore[arg-type]


def test_timeouts_on_two_servers_still_cut_the_execution_cap() -> None:
    pol = AdaptivePolicy(params_from_config(KiroCrewConfig(), exec_ceiling=10))
    d = pol.observe(
        _clean(
            0.0,
            running=10,
            healthy_in_flight=6,
            attributable_timeout_rate=0.4,
            slow_or_failing_keys=2,
        )
    )
    assert d.effective_exec_cap == 6


def test_fd_and_process_exhaustion_still_cut_the_execution_cap() -> None:
    pol = AdaptivePolicy(params_from_config(KiroCrewConfig(), exec_ceiling=10))
    d = pol.observe(
        _clean(0.0, running=10, fd_count=900, fd_limit=1000, proc_count=95, proc_limit=100)
    )
    assert {SIGNAL_FDS, SIGNAL_PROCS} <= set(d.signals)
    assert d.effective_exec_cap == 5


def test_a_provider_429_still_never_cuts_the_execution_cap() -> None:
    pol = AdaptivePolicy(params_from_config(KiroCrewConfig(), exec_ceiling=10))
    for i in range(6):
        d = pol.observe(_clean(i * 31.0, per_provider_429={"provider:x": 40}, running=10))
        assert d.effective_exec_cap == 10
        assert d.throttled_providers == ("provider:x",)


# ── (vi) the model is told a defined figure ─────────────────────────────────


def test_the_prompt_figure_is_the_auto_ceiling_on_any_host(monkeypatch, host_gb) -> None:
    from kiro_crew import resource_status
    from kiro_crew.context import ContextBuilder

    cfg = KiroCrewConfig()
    monkeypatch.setattr(KiroCrewConfig, "load", lambda: cfg)
    monkeypatch.setattr(resource_status, "adaptive_exec_cap", lambda: 0)
    host_gb(8.0)
    assert ContextBuilder._live_cap_figure() == (
        f"{cfg.agent.subagent_auto_max} (configured ceiling)"
    )
    # The cap in force, when a controller runs here, is printed as is.
    monkeypatch.setattr(resource_status, "adaptive_exec_cap", lambda: 32)
    assert ContextBuilder._live_cap_figure() == "32"


def test_the_spawn_tool_names_the_figure_and_the_memory_wait(monkeypatch, host_gb) -> None:
    from kiro_crew.mcp_tools import spawn as spawn_tools

    cfg = KiroCrewConfig()
    monkeypatch.setattr(spawn_tools.KiroCrewConfig, "load", lambda: cfg)
    monkeypatch.setattr(spawn_tools.host_status, "adaptive_exec_cap", lambda: 0)
    host_gb(8.0)
    tools = {t["name"]: t for t in spawn_tools.schemas()}
    text = tools["spawn_sub_agents"]["description"]
    assert f"Your configured sub-agent ceiling is {cfg.agent.subagent_auto_max};" in text
    assert "each start also waits until host memory can hold it" in text


# ── (vii) the TaskRunner keeps a non-zero auto value ────────────────────────


def test_the_taskrunner_auto_value_is_never_zero(host_gb) -> None:
    from kiro_crew.taskrunner import TaskRunner

    cfg = KiroCrewConfig()
    for gb in (-1.0, 0.5, 8.0, 200.0):
        host_gb(gb)
        auto = TaskRunner._clamp_parallel_steps(0, cfg)
        assert 3 <= auto <= cfg.agent.subagent_auto_max, (gb, auto)
    assert TaskRunner._clamp_parallel_steps(0, None) > 0
