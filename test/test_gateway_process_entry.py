"""The background work ``run_gateway`` starts, ``gateway_runtime/process_entry.py``.

Outside test mode ``run_gateway`` schedules contained background tasks before it
builds the orchestrator: the KAS kiro-cli floor, the aggregate cgroup ceiling, the
agents-dir janitor, the hourly scratch/work-root/session-dir/read-only-spec sweep
and the kiro-cli log cap. Each loop sleeps first and swallows its own failures.
These tests run ``run_gateway`` against a stub orchestrator with every sweep
replaced, let each loop wake exactly once, and pin what each wake calls and that a
failing step neither stops the next step nor escapes the task.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import agent as agent_mod
from kiro_crew.agent_sdk.drivers import acp as acp_driver
from kiro_crew.dashboard import side_readonly_spec
from kiro_crew.slack import gateway as gw

_TASK_GLOBALS = (
    "_KAS_CLI_FLOOR_TASK",
    "_SLICE_LIMITS_TASK",
    "_AGENTS_JANITOR_TASK",
    "_AGENT_SCRATCH_SWEEP_TASK",
    "_KIRO_CLI_LOG_CAP_TASK",
)


class _OneWakeAsyncio:
    """The facade's ``asyncio`` with a ``sleep`` that wakes each task once.

    The first ``sleep`` a task makes returns at once; its second cancels the task,
    so a ``while True`` loop runs its body exactly once and then ends. Everything
    else is the real module.
    """

    def __init__(self) -> None:
        self._wakes: dict[Any, int] = {}

    def __getattr__(self, name: str) -> Any:
        return getattr(asyncio, name)

    async def sleep(self, _delay: float, *_args: Any) -> None:
        task = asyncio.current_task()
        self._wakes[task] = self._wakes.get(task, 0) + 1
        if self._wakes[task] > 1:
            raise asyncio.CancelledError


class _InlineThread:
    """``threading.Thread`` that runs its target when started, on this thread."""

    def __init__(self, *, target: Any, args: tuple = (), kwargs: dict | None = None, **_kw: Any):
        self._call = (target, args, kwargs or {})

    def start(self) -> None:
        target, args, kwargs = self._call
        target(*args, **kwargs)


def _seams(monkeypatch: pytest.MonkeyPatch, *, fail: bool) -> SimpleNamespace:
    """Replace every effect ``run_gateway`` reaches outside test mode."""

    def _step(name: str) -> MagicMock:
        return MagicMock(name=name, side_effect=RuntimeError(name) if fail else None)

    seams = SimpleNamespace(
        kas_floor=_step("apply_kas_cli_floor"),
        slice_limits=_step("ensure_agents_slice_limits"),
        sweep_agents=_step("sweep_agents_dir"),
        reap=_step("reap_orphaned_readonly_specs"),
        drain_aliases=MagicMock(name="drain_skill_view_aliases"),
        retry_specs=_step("retry_held_conductor_specs"),
        scratch=_step("sweep_dead_scratch"),
        work_root=_step("sweep_work_root"),
        session_dirs=_step("_sweep_predecessor_session_work_dirs"),
        log_cap=_step("cap_kiro_cli_logs"),
        beacon=MagicMock(name="beacon.send"),
        orchestrator=MagicMock(name="orchestrator"),
    )
    seams.orchestrator.run = AsyncMock()
    seams.orchestrator.sessions = None
    for name in _TASK_GLOBALS:
        monkeypatch.setattr(gw, name, None)
    monkeypatch.setattr(gw, "asyncio", _OneWakeAsyncio())
    monkeypatch.setattr(gw, "threading", SimpleNamespace(Thread=_InlineThread))
    monkeypatch.setattr(gw, "configure_default_executor", MagicMock())
    monkeypatch.setattr(gw, "set_publish_disabled", MagicMock())
    monkeypatch.setattr(gw, "boot_platform", MagicMock())
    monkeypatch.setattr(gw, "apply_kas_cli_floor", seams.kas_floor)
    monkeypatch.setattr(gw, "ensure_agents_slice_limits", seams.slice_limits)
    monkeypatch.setattr(gw, "sweep_agents_dir", seams.sweep_agents)
    monkeypatch.setattr(gw, "kiro_agents_dir", MagicMock(return_value="agents-dir"))
    monkeypatch.setattr(side_readonly_spec, "reap_orphaned_readonly_specs", seams.reap)
    monkeypatch.setattr(acp_driver, "drain_skill_view_aliases", seams.drain_aliases)
    monkeypatch.setattr(agent_mod, "retry_held_conductor_specs", seams.retry_specs)
    monkeypatch.setattr(
        gw,
        "agent_scratch",
        SimpleNamespace(
            sweep_dead_scratch=seams.scratch,
            cap_kiro_cli_logs=seams.log_cap,
            KIRO_CLI_LOG_CAP_INTERVAL_SECONDS=300,
        ),
    )
    monkeypatch.setattr(gw, "work_root", SimpleNamespace(sweep_work_root=seams.work_root))
    monkeypatch.setattr(gw, "_sweep_predecessor_session_work_dirs", seams.session_dirs)
    monkeypatch.setattr(gw, "beacon", SimpleNamespace(send=seams.beacon))
    monkeypatch.setattr(gw, "GatewayOrchestrator", MagicMock(return_value=seams.orchestrator))
    return seams


async def _run_and_settle() -> None:
    """Run ``run_gateway`` outside test mode, then wait for every task it started."""
    cfg = MagicMock(name="cfg")
    await gw.run_gateway(cfg)
    tasks = [getattr(gw, name) for name in _TASK_GLOBALS]
    assert all(isinstance(task, asyncio.Task) for task in tasks), tasks
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_each_background_loop_wakes_and_runs_every_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seams = _seams(monkeypatch, fail=False)

    await _run_and_settle()

    seams.kas_floor.assert_called_once()
    seams.slice_limits.assert_called_once_with()
    seams.sweep_agents.assert_called_once()
    seams.drain_aliases.assert_called_once_with()
    # The boot janitor reaps with no live names; the hourly wake passes them. The two
    # run on separate executor threads, so their order is not asserted.
    reaps = [c.args for c in seams.reap.call_args_list]
    assert len(reaps) == 2 and () in reaps and (set(),) in reaps
    seams.retry_specs.assert_called_once_with()
    seams.scratch.assert_called_once_with()
    seams.work_root.assert_called_once_with()
    seams.session_dirs.assert_called_once_with([])
    seams.log_cap.assert_called_once_with()
    seams.beacon.assert_called_once()
    seams.orchestrator.run.assert_awaited_once()
    seams.orchestrator._stop_memory_startup.assert_called_once_with()


@pytest.mark.asyncio
async def test_a_failing_step_is_logged_and_the_next_step_still_runs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    seams = _seams(monkeypatch, fail=True)

    with caplog.at_level(logging.DEBUG):
        await _run_and_settle()

    # Every hourly step ran although the one before it raised.
    for step in (seams.retry_specs, seams.scratch, seams.work_root, seams.session_dirs):
        step.assert_called_once()
    assert seams.reap.call_count == 1  # the janitor's reap is never reached: its sweep raised
    seams.log_cap.assert_called_once_with()
    for line in (
        "KAS kiro-cli floor check failed; continuing",
        "aggregate cgroup ceiling apply failed",
        "agents-dir janitor sweep failed at boot",
        "held conductor spec retry failed",
        "agent-scratch sweep failed",
        "work-root sweep failed",
        "session work-dir sweep failed",
        "read-only spec reap failed",
        "kiro-cli log cap failed",
    ):
        assert line in caplog.text, line
