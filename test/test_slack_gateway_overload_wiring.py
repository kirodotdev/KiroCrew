"""The gateway publishes the adaptive controller's caps, the degrade reason and
the manager's dependency coordinator at start, and withdraws them at shutdown.

``session_health`` renders ``effective_caps`` and ``degrade_reason`` from the
sources registered here; ``taskq.dependency.current_coordinator`` is how a
caller without a task row reads a scope's shared ``retry_at``. Without this
wiring both surfaces are empty in the running gateway even though every
producer exists.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew.dashboard import session_health
from kiro_crew.slack.gateway import GatewayOrchestrator
from kiro_crew.taskq import dependency as taskq_dependency


class _Controller:
    def __init__(self, **state):
        self._state = {
            "enabled": True,
            "spawn_gate_capacity": 4,
            "applied_gate_cap": 4,
            "gate_pending": None,
            "gate_floor": 1,
            "gate_ceiling": 8,
            "effective_exec_cap": 6,
            "exec_ceiling": 10,
            "paused": False,
            "probing": False,
            "last": None,
        }
        self._state.update(state)

    def state(self) -> dict:
        return dict(self._state)


@pytest.fixture
def clean_sources():
    monitor = session_health.default_monitor()
    monitor.clear_sources()
    taskq_dependency.register_coordinator(None)
    yield monitor
    monitor.clear_sources()
    taskq_dependency.register_coordinator(None)


def _orch(coordinator=None) -> GatewayOrchestrator:
    orch = GatewayOrchestrator.__new__(GatewayOrchestrator)
    orch.subagent_mgr = SimpleNamespace(dependency_coordinator=lambda: coordinator)
    return orch


def _caps(monitor) -> dict:
    snapshot = session_health.HealthSnapshot()
    return monitor._effective_caps(snapshot)


def test_caps_and_reason_come_from_the_controller(clean_sources) -> None:
    monitor = clean_sources
    orch = _orch()
    orch._wire_overload_health(_Controller(last={"action": "decrease"}))
    caps = _caps(monitor)
    assert caps["spawn_gate"]["effective"] == 4
    assert caps["spawn_gate"]["ceiling"] == 8
    assert caps["subagents"]["adaptive"] == 6
    assert monitor._degrade_reason() == "adaptive_decrease"


@pytest.mark.parametrize(
    "state,reason",
    [
        ({"paused": True}, "adaptive_pause"),
        ({"probing": True}, "adaptive_probe"),
        ({"last": {"action": "hold"}}, None),
        ({"last": {"action": "increase"}}, None),
    ],
)
def test_degrade_reason_is_a_closed_set_token(clean_sources, state, reason) -> None:
    orch = _orch()
    orch._wire_overload_health(_Controller(**state))
    assert clean_sources._degrade_reason() == reason


def test_disabled_controller_publishes_no_caps(clean_sources) -> None:
    orch = _orch()
    orch._wire_overload_health(_Controller(enabled=False))
    assert "spawn_gate" not in _caps(clean_sources)
    assert clean_sources._degrade_reason() is None


def test_coordinator_is_registered_and_withdrawn(clean_sources) -> None:
    coordinator = object()
    orch = _orch(coordinator)
    orch._wire_overload_health(_Controller())
    assert taskq_dependency.current_coordinator() is coordinator
    orch._unwire_overload_health()
    assert taskq_dependency.current_coordinator() is None
    assert _caps(clean_sources) == {}
    assert clean_sources._degrade_reason() is None


def test_manager_without_a_coordinator_leaves_the_handle_empty(clean_sources) -> None:
    orch = GatewayOrchestrator.__new__(GatewayOrchestrator)
    orch.subagent_mgr = SimpleNamespace()
    orch._wire_overload_health(_Controller())
    assert taskq_dependency.current_coordinator() is None


def test_adaptive_controller_is_not_started_during_subagent_init():
    import inspect

    source = inspect.getsource(GatewayOrchestrator._init_subagents)
    assert "_start_adaptive_controller" not in source


def test_importing_the_gateway_loads_no_adaptive_module(tmp_path) -> None:
    """The boot path stays clear of a subsystem a disabled switch never uses.

    ``agent.adaptive_concurrency`` is checked inside
    ``_start_adaptive_controller``, which runs long after the socket binds; an
    import at ``slack.gateway`` module scope is paid before it, by every
    launch, whatever the switch says. A subprocess, because ``sys.modules`` in
    this one is already whatever the rest of the suite imported.
    """
    import os
    import subprocess
    import sys
    from pathlib import Path

    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    env["KIROCREW_HOME"] = str(tmp_path / "crew")
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            "import sys, importlib; importlib.import_module('kiro_crew.slack.gateway'); "
            "leaked = sorted(m for m in sys.modules if m.startswith('kiro_crew.adaptive')); "
            "assert not leaked, leaked",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_disabled_gateway_constructs_controller_only_on_live_enable(monkeypatch):
    from unittest.mock import MagicMock

    from kiro_crew.adaptive import controller as adaptive_controller
    from kiro_crew.config import live
    from kiro_crew.config.loader import KiroCrewConfig

    orch = _orch()
    orch._adaptive_controller = None
    orch._cfg = KiroCrewConfig()
    orch._cfg.agent.adaptive_concurrency = False
    monkeypatch.setattr(orch, "_wire_overload_health", MagicMock())
    factory = MagicMock()
    # Patched on the adaptive module, not on ``gateway``: the enabled branch
    # imports the name when it runs, so ``gateway`` holds no binding to shadow.
    monkeypatch.setattr(adaptive_controller, "AdaptiveController", factory)
    monkeypatch.setattr(adaptive_controller, "register", MagicMock())
    watch = live.ConfigWatch()
    monkeypatch.setattr(live, "watch_object", watch.watch_object)
    orch._start_adaptive_controller()
    orch._start_adaptive_controller()
    factory.assert_not_called()
    subscriptions = list(watch.subscriptions())
    assert len(subscriptions) == 1
    enabled = KiroCrewConfig()
    enabled.agent.adaptive_concurrency = True
    enabled.agent.adaptive_initial = 7
    callback = subscriptions[0].callback()
    assert callback is not None
    result = callback(
        live.ConfigChange(
            old=orch._cfg, new=enabled, changed=frozenset({"agent.adaptive_concurrency"})
        )
    )
    if result is not None:
        await result
    factory.assert_called_once()
    assert factory.call_args.kwargs["cfg"] is enabled
    factory.return_value.start.assert_called_once()
    assert orch._adaptive_controller is factory.return_value
    assert list(watch.subscriptions()) == []
    orch._start_adaptive_controller(enabled)
    factory.assert_called_once()


def _started_controller_kwargs(monkeypatch, orch) -> dict:
    from unittest.mock import MagicMock

    from kiro_crew.adaptive import controller as adaptive_controller
    from kiro_crew.config.loader import KiroCrewConfig

    orch._adaptive_controller = None
    orch._cfg = KiroCrewConfig()
    orch._cfg.agent.adaptive_concurrency = True
    monkeypatch.setattr(orch, "_wire_overload_health", MagicMock())
    factory = MagicMock()
    monkeypatch.setattr(adaptive_controller, "AdaptiveController", factory)
    monkeypatch.setattr(adaptive_controller, "register", MagicMock())
    orch._start_adaptive_controller()
    factory.assert_called_once()
    assert orch._adaptive_controller is factory.return_value
    return factory.call_args.kwargs


def test_active_session_reader_counts_live_providers_and_never_raises(monkeypatch) -> None:
    orch = _orch()
    orch.sessions = None
    read = _started_controller_kwargs(monkeypatch, orch)["read_active_sessions"]
    assert read() == -1

    orch.sessions = SimpleNamespace(active_providers=lambda: [object(), object(), object()])
    assert read() == 3

    def _broken():
        raise RuntimeError("allocation boundary gone")

    orch.sessions = SimpleNamespace(active_providers=_broken)
    assert read() == -1


def test_cron_queue_reader_is_the_executor_depth(monkeypatch) -> None:
    from kiro_crew import executors

    orch = _orch()
    orch.sessions = None
    read = _started_controller_kwargs(monkeypatch, orch)["read_cron_queue"]
    monkeypatch.setattr(executors, "cron_queue_depth", lambda: 7)
    assert read() == 7


def test_runner_lane_reader_follows_the_live_admission(monkeypatch) -> None:
    orch = _orch()
    orch.sessions = None
    read = _started_controller_kwargs(monkeypatch, orch)["read_runner_lane"]
    assert read() is None

    orch._runner_admission = SimpleNamespace(lane=None)
    assert read() is None

    orch._runner_admission = SimpleNamespace(
        lane=SimpleNamespace(stats=lambda: {"running": 2, "queued": 1})
    )
    assert read() == {"running": 2, "queued": 1}

    def _broken():
        raise RuntimeError("lane torn down")

    orch._runner_admission = SimpleNamespace(lane=SimpleNamespace(stats=_broken))
    assert read() is None


@pytest.mark.asyncio
async def test_gate_callbacks_forward_to_the_live_broker_or_report_none(monkeypatch) -> None:
    orch = _orch()
    orch.sessions = None
    orch._mcp_gateway_manager = None
    kwargs = _started_controller_kwargs(monkeypatch, orch)
    assert await kwargs["set_gate_capacity"](3) is None
    assert await kwargs["read_gate_stats"]() == {}

    applied: list[int] = []

    async def set_spawn_capacity(capacity: int) -> int:
        applied.append(capacity)
        return capacity

    async def stats() -> dict:
        return {"admission": {"spawn_gate": {"queued": 4}}}

    orch._mcp_gateway_manager = SimpleNamespace(set_spawn_capacity=set_spawn_capacity, stats=stats)
    assert await kwargs["set_gate_capacity"](3) == 3
    assert applied == [3]
    assert await kwargs["read_gate_stats"]() == {"admission": {"spawn_gate": {"queued": 4}}}


@pytest.mark.asyncio
@pytest.mark.parametrize("no_dashboard", [False, True])
async def test_run_starts_adaptive_only_after_dashboard_and_taskq_ready(
    monkeypatch, tmp_path, capsys, no_dashboard
):
    import asyncio
    import inspect
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew import session
    from kiro_crew.metrics import sessions as session_metrics
    from kiro_crew.slack import events, gateway

    class ReachedController(Exception):
        pass

    orch = GatewayOrchestrator.__new__(GatewayOrchestrator)
    orch._background_tasks = set()
    # The boot work-dir sweep reads this flag before the adaptive controller
    # starts; True skips it, as the offline E2E gate does.
    orch._test_mode = True
    orch._no_dashboard = no_dashboard
    orch._json_ready = True
    orch._owner_id = ""
    orch._dashboard_port = 0
    # ``run`` reads the dashboard state before the memory-backed writers start
    # (the crewmate-prune barrier); a bare orchestrator has none, as the
    # ``__init__`` this test skips would have set.
    orch.dashboard_state = None
    order = []

    async def bind_dashboard():
        order.append("bound")

    async def memory_ready():
        assert "KIROCREW_READY:" in capsys.readouterr().out
        order.append("ready")
        return True

    async def taskq_ready():
        order.append("taskq")

    async def build_coordinator():
        order.append("coordinator")

    def wire_runner():
        order.append("wire")

    async def store_ready():
        order.append("store")

    def release_dispatch():
        order.append("dispatch")

    def start_adaptive():
        order.append("adaptive")
        raise ReachedController

    monkeypatch.setattr(
        orch,
        "subagent_mgr",
        SimpleNamespace(
            wait_taskq_ready=AsyncMock(side_effect=taskq_ready),
            dependency_coordinator_async=AsyncMock(side_effect=build_coordinator),
            release_queue_dispatch=MagicMock(side_effect=release_dispatch),
        ),
        raising=False,
    )
    for method in (
        "_init_services",
        "_start_embeddings",
        "_init_mcp_gateway",
        "_init_cron",
    ):
        monkeypatch.setattr(orch, method, AsyncMock())
    for method in (
        "_init_mcp_discovery",
        "_init_subagents",
        "_init_task_runner",
        "_schedule_console_script_repair",
        "_wire_mcp_gateway_dashboard",
        "_install_shutdown_signal_handlers",
    ):
        monkeypatch.setattr(orch, method, MagicMock())
    monkeypatch.setattr(orch, "_wire_runner_admission", MagicMock(side_effect=wire_runner))
    monkeypatch.setattr(orch, "_runner_admission_store_ready", AsyncMock(side_effect=store_ready))
    monkeypatch.setattr(orch, "_init_dashboard", AsyncMock(side_effect=bind_dashboard))
    monkeypatch.setattr(orch, "_init_api_server", AsyncMock(side_effect=bind_dashboard))
    monkeypatch.setattr(orch, "_wait_for_memory_preparation", AsyncMock(side_effect=memory_ready))
    monkeypatch.setattr(orch, "_start_adaptive_controller", MagicMock(side_effect=start_adaptive))
    monkeypatch.setattr(gateway.crash_guard, "install_loop_handler", MagicMock())
    monkeypatch.setattr(gateway.platform_compat, "raise_nofile_soft_limit", MagicMock())
    monkeypatch.setattr(gateway.platform_compat, "probe_file_persistence", lambda home: None)
    monkeypatch.setattr(gateway, "data_home", lambda: tmp_path)
    monkeypatch.setattr(gateway, "generate_token", lambda *args, **kwargs: "test-ready")
    monkeypatch.setattr(gateway, "warm_backend", MagicMock())
    monkeypatch.setattr(session, "cleanup_orphaned_sessions", MagicMock())
    monkeypatch.setattr(session_metrics, "backfill_crashed_sessions", lambda cutoff: 0)
    monkeypatch.setattr(gateway.cautious_boot, "initialize", AsyncMock())
    monkeypatch.setattr(gateway.cautious_boot, "pause_before", AsyncMock())
    monkeypatch.setattr(events, "SeenCache", MagicMock())
    try:
        with pytest.raises(ReachedController):
            await orch.run()
        # The runner admission is wired once the socket is bound and the
        # READY line is out, so both consumers can refuse typed, and bound to
        # its store between the store barrier and the controller -- while the
        # runner is still idle, because the adoption sweep settles any unleased
        # ACTIVE row.
        # The dependency coordinator is built on the store's writer thread
        # before EACH loop-side wiring pass reads it: its first build rebuilds
        # the wait schedule from every waiting row, which is also why the pass
        # sits AFTER the READY print (`test_memory_startup` pins that).
        # "ready" below is the MEMORY barrier, not the READY line, and
        # "dispatch" is the durable subagent queue opening after it: a recovered
        # row admitted before the barrier starts without its memory store.
        assert order == [
            "bound",
            "coordinator",
            "wire",
            "ready",
            "taskq",
            "store",
            "coordinator",
            "dispatch",
            "adaptive",
        ]
        assert (
            inspect.getsource(GatewayOrchestrator.run).count("self._start_adaptive_controller(")
            == 1
        )
    finally:
        await asyncio.gather(*tuple(orch._background_tasks))
