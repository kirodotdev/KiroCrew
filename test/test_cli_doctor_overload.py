"""``kirocrew doctor`` prints the overload-resilience contract: per-state task
counts, the configured admission bounds, the recovery ladder and the platform
liveness evidence.

Doctor is a separate process, so it prints CONFIGURED bounds and static
platform facts and points at ``GET /api/sessions/health`` for the live counts;
it must never print a live-looking number it cannot read.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew import cli_doctor
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.recovery import ladder as lad
from kiro_crew.taskq import TaskRecord, TaskStore


@pytest.fixture(autouse=True)
def _fresh_process_ladder():
    """``_doctor_overload_resilience`` installs this process's recovery ladder,
    which is a module global, so a non-default schedule set here must not reach
    a sibling test sharing the worker."""
    lad._reset_default_ladder_for_tests()
    yield
    lad._reset_default_ladder_for_tests()


def _rec(task_id: str) -> TaskRecord:
    return TaskRecord(id=task_id, kind="subagent", params={})


def test_task_store_section_lists_per_state_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    store = TaskStore(tmp_path / "tasks" / "tasks.db", network_fs=False).open()
    store.accept([_rec("a"), _rec("b")])
    store.close()
    issues: list[str] = []
    cli_doctor._doctor_task_store(issues)
    out = capsys.readouterr().out
    assert "pending=2" in out
    assert "task states: queued=2" in out
    assert issues == []


def test_overload_section_prints_configured_bounds(capsys: pytest.CaptureFixture[str]) -> None:
    cfg = KiroCrewConfig()
    cfg.agent.session_start_concurrency = 3
    cfg.agent.adaptive_concurrency_mode = "fixed"
    cfg.agent.interactive_command_policy = "wait"
    cfg.mcp_gateway.spawn_concurrency_initial = 5
    cli_doctor._doctor_overload_resilience(cfg)
    out = capsys.readouterr().out
    assert "session_start_concurrency=3" in out
    assert "spawn_gate=5 [1..8]" in out
    assert "GET /api/sessions/health" in out
    assert "adaptive concurrency: fixed" in out
    assert "L1_tool_call" in out and "L4_gatewayd" in out and "L5_gateway" in out
    assert "policy=wait" in out
    assert "liveness evidence:" in out


def test_ladder_rows_print_the_configured_backoff(capsys: pytest.CaptureFixture[str]) -> None:
    """One knob, one number: the rows and the dependency line cannot disagree."""
    cfg = KiroCrewConfig()
    cfg.agent.recovery_backoff_base_secs = 7.0
    cfg.agent.recovery_backoff_max_secs = 300.0
    cli_doctor._doctor_overload_resilience(cfg)
    out = capsys.readouterr().out
    assert "L1_tool_call: backoff 7s→300s" in out
    assert "L3_acp_runtime: backoff 7s→300s" in out
    assert "dependency waits: backoff 7s→300s" in out
    assert "L4_gatewayd: backoff 1s→60s" in out  # pinned
    assert "backoff 2s→120s" not in out
    # Printed off the same process ladder a consumer decides a retry on.
    assert lad.default_ladder().layer_policy(lad.L1_TOOL_CALL).base_secs == 7.0


def test_overload_section_marks_adaptive_off(capsys: pytest.CaptureFixture[str]) -> None:
    cfg = KiroCrewConfig()
    cfg.agent.adaptive_concurrency = False
    cli_doctor._doctor_overload_resilience(cfg)
    assert "adaptive concurrency: off" in capsys.readouterr().out


@pytest.mark.parametrize(
    "platform,fragment",
    [
        ("linux", "STUCK_INPUT (blocked"),
        ("darwin", "STUCK_INPUT and socket evidence absent"),
        ("win32", "no process-tree backend"),
    ],
)
def test_liveness_line_declares_the_platform_degradation(
    monkeypatch: pytest.MonkeyPatch, platform: str, fragment: str
) -> None:
    monkeypatch.setattr(cli_doctor.sys, "platform", platform)
    line = cli_doctor._liveness_platform_line()
    assert fragment in line
    if platform != "linux":
        assert "platform_limited" in line
