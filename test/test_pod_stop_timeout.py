"""A pod stop is waited on for as long as systemd may take to finish it.

``systemctl --user stop`` returns when the stop job ends. The pod unit sets
``TimeoutStopSec``, and each wait above it (``systemctl``, ``kirocrew pod down``
from Dev Fleet, the ``pod_down`` MCP call) is longer than the one below, so a
pod that is still stopping is never reported as a failed stop.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pod_helpers import (  # noqa: F401 -- the fixtures register by import
    _cp,
    _isolate_pod_host_state,
    _systemd_backend,
    cfg_fixture,
    requires_posix_pod_lifecycle,
)

from kiro_crew.apps.builtins.dev_fleet import repository as repository_mod
from kiro_crew.apps.builtins.dev_fleet import runtime as runtime_mod
from kiro_crew.apps.builtins.dev_fleet import worktree_ops_pods
from kiro_crew.apps.manifest import AppManifest
from kiro_crew.pod import runtime as rt
from kiro_crew.pod import unit as unit_mod
from kiro_crew.pod.config import POD_STOP_TIMEOUT_SECS, SYSTEMCTL_STOP_WAIT_SECS, PodConfig
from kiro_crew.pod.runtime_lifecycle import DRAIN_TIMEOUT_SECS


def test_the_unit_sets_its_own_stop_timeout(cfg: PodConfig) -> None:
    lines = unit_mod.render_unit(cfg).splitlines()
    assert f"TimeoutStopSec={POD_STOP_TIMEOUT_SECS}" in lines
    # An installed unit without it is stale, so the next start re-renders it.
    assert "TimeoutStopSec=" in unit_mod._REQUIRED_DIRECTIVES


def test_the_stop_wait_covers_sigterm_and_sigkill() -> None:
    assert SYSTEMCTL_STOP_WAIT_SECS > 2 * POD_STOP_TIMEOUT_SECS


def _recording_systemctl(calls: list[tuple[tuple[str, ...], object]]):
    def _systemctl(*args: str, **kwargs: object) -> subprocess.CompletedProcess:
        calls.append((args, kwargs.get("timeout")))
        return _cp()

    return _systemctl


def _stop_timeouts(calls: list[tuple[tuple[str, ...], object]]) -> list[object]:
    return [timeout for args, timeout in calls if args[0] == "stop"]


@requires_posix_pod_lifecycle
def test_down_waits_for_the_whole_stop(cfg: PodConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[tuple[str, ...], object]] = []
    monkeypatch.setattr(rt, "systemctl", _recording_systemctl(calls))
    monkeypatch.setattr(rt, "loaded_teardown_hook", lambda c, n: False)
    monkeypatch.setattr(rt.unit_mod, "unit_is_current", lambda c: True)
    monkeypatch.setattr(rt, "cgroup_procs_file", lambda c, n: None)
    monkeypatch.setattr(rt, "cleanup_home", lambda c, n: 0)
    assert rt.stop_pod(cfg, "demo").returncode == 0
    assert _stop_timeouts(calls) == [SYSTEMCTL_STOP_WAIT_SECS]


@requires_posix_pod_lifecycle
def test_halt_waits_for_the_whole_stop(cfg: PodConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[tuple[str, ...], object]] = []
    monkeypatch.setattr(rt, "systemctl", _recording_systemctl(calls))
    monkeypatch.setattr(rt, "loaded_teardown_hook", lambda c, n: False)
    monkeypatch.setattr(rt.unit_mod, "unit_is_current", lambda c: True)
    assert rt.halt_pod(cfg, "demo").returncode == 0
    assert _stop_timeouts(calls) == [SYSTEMCTL_STOP_WAIT_SECS]


@requires_posix_pod_lifecycle
@pytest.mark.parametrize("current", [False, True], ids=["stale", "current"])
def test_down_refreshes_a_stale_hookless_unit_before_stopping(
    cfg: PodConfig, monkeypatch: pytest.MonkeyPatch, current: bool
) -> None:
    order: list[str] = []

    def _systemctl(*args: str, **kwargs: object) -> subprocess.CompletedProcess:
        order.append(args[0])
        return _cp()

    monkeypatch.setattr(rt, "loaded_teardown_hook", lambda c, n: False)
    monkeypatch.setattr(rt.unit_mod, "unit_is_current", lambda c: current)
    monkeypatch.setattr(rt.unit_mod, "install_unit", lambda c: order.append("re-render"))
    monkeypatch.setattr(rt, "systemctl", _systemctl)
    monkeypatch.setattr(rt, "cgroup_procs_file", lambda c, n: None)
    monkeypatch.setattr(rt, "cleanup_home", lambda c, n: 0)
    assert rt.stop_pod(cfg, "demo").returncode == 0
    assert order == (["stop"] if current else ["re-render", "daemon-reload", "stop"])


@requires_posix_pod_lifecycle
@pytest.mark.parametrize("current", [False, True], ids=["stale", "current"])
def test_halt_refreshes_a_stale_hookless_unit_before_stopping(
    cfg: PodConfig, monkeypatch: pytest.MonkeyPatch, current: bool
) -> None:
    order: list[str] = []

    def _systemctl(*args: str, **kwargs: object) -> subprocess.CompletedProcess:
        order.append(args[0])
        return _cp()

    monkeypatch.setattr(rt, "loaded_teardown_hook", lambda c, n: False)
    monkeypatch.setattr(rt.unit_mod, "unit_is_current", lambda c: current)
    monkeypatch.setattr(rt.unit_mod, "install_unit", lambda c: order.append("re-render"))
    monkeypatch.setattr(rt, "systemctl", _systemctl)
    assert rt.halt_pod(cfg, "demo").returncode == 0
    assert order == (["stop"] if current else ["re-render", "daemon-reload", "stop"])


def test_each_wait_above_the_stop_is_longer() -> None:
    cli_bound = worktree_ops_pods._POD_DOWN_TIMEOUT_SECS
    assert cli_bound > SYSTEMCTL_STOP_WAIT_SECS + DRAIN_TIMEOUT_SECS
    # The gateway's bound must fire first: it is the one that names the cause. The
    # pod_down tool's wait is the one Dev Fleet's shipped manifest declares.
    manifest = AppManifest.from_json_file(Path(worktree_ops_pods.__file__).parent / "app.json")
    waits = {t.name: t.timeout_seconds for t in manifest.agentTools.tools}
    assert waits["pod_down"] > cli_bound


@pytest.mark.asyncio
async def test_dev_fleet_gives_pod_down_its_whole_bound(tmp_path) -> None:
    run_cmd = AsyncMock(return_value=(0, "", ""))
    with (
        patch.object(
            repository_mod,
            "_find_worktree",
            new_callable=AsyncMock,
            return_value=({"path": str(tmp_path)}, None),
        ),
        patch.object(worktree_ops_pods, "_pod_checkout_guard", AsyncMock(return_value=None)),
        patch.object(worktree_ops_pods, "_pod_env", return_value={}),
        patch.object(repository_mod, "_repo", return_value=str(tmp_path)),
        patch.object(runtime_mod, "_warm_build_path", AsyncMock()),
        patch.object(runtime_mod, "_find_cli", return_value=["kirocrew"]),
        patch.object(runtime_mod, "_run_cmd", run_cmd),
        patch.object(runtime_mod, "_load_cfg", return_value=SimpleNamespace()),
        patch.object(runtime_mod, "_POD_AVAILABLE", False),
    ):
        result = await worktree_ops_pods._pod_down("demo")
    assert result == {"ok": True, "error": None}
    assert run_cmd.await_args is not None
    assert run_cmd.await_args.kwargs["timeout"] == worktree_ops_pods._POD_DOWN_TIMEOUT_SECS
