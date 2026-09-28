"""The startup watchdog's window is sized from the session-start budget.

One start clock runs through ``session/new`` (``agent.session_start_timeout_secs``)
and, when that times out, the late-start collector's wait for a late answer
(``agent.start_collect_timeout_secs`` plus the grace ``_await_late_start`` adds).
The watchdog (``SubagentManager._is_startup_stalled``) must give a start at
least that long, whatever the configured budgets, and a config write must not
move the window of a start already in flight. Budgets are stubbed on the live
config snapshot, the same values the runtime's own start path reads.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kiro_crew.config import live
from kiro_crew.subagent import (
    _STARTUP_COLLECT_GRACE_SECS,
    _STARTUP_LAUNCH_MARGIN_SECS,
    SubagentInfo,
    SubagentManager,
)

NOW = 10_000.0
COLLECT = 300


def _manager(**kwargs) -> SubagentManager:
    return SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock(), **kwargs)


def _budget(monkeypatch: pytest.MonkeyPatch, secs: float | None, collect: int = COLLECT) -> None:
    cfg = (
        None
        if secs is None
        else SimpleNamespace(
            agent=SimpleNamespace(
                session_start_timeout_secs=secs, start_collect_timeout_secs=collect
            )
        )
    )
    monkeypatch.setattr(live, "snapshot", lambda: cfg)


def _starting(now: float, elapsed: float) -> SubagentInfo:
    info = SubagentInfo(id="a1b2c3d4", task="t", agent="")
    info._exec_started = now - elapsed
    info._pid = None
    info.turns = 0
    return info


def _one_clock(budget: float, collect: int = COLLECT) -> float:
    return budget + collect + _STARTUP_COLLECT_GRACE_SECS + _STARTUP_LAUNCH_MARGIN_SECS


@pytest.mark.parametrize("budget", [90, 900])
def test_deadline_covers_session_new_plus_the_collector_wait(monkeypatch, budget):
    _budget(monkeypatch, budget)
    assert _manager()._startup_deadline >= _one_clock(budget)


@pytest.mark.parametrize("budget", [90, 900])
def test_slow_start_inside_the_budget_is_not_reaped(monkeypatch, budget):
    _budget(monkeypatch, budget)
    assert _manager()._is_startup_stalled(_starting(NOW, budget + 20), NOW) is False


@pytest.mark.parametrize("budget", [90, 900])
def test_start_past_the_window_is_reaped(monkeypatch, budget):
    _budget(monkeypatch, budget)
    mgr = _manager()
    assert mgr._is_startup_stalled(_starting(NOW, _one_clock(budget) + 1), NOW) is True


def test_late_answer_inside_the_collector_wait_is_not_reaped(monkeypatch):
    """session/new times out at 90 s; the collector adopts a late answer at 390 s."""
    _budget(monkeypatch, 90)
    mgr = _manager()
    info = _starting(NOW, 389)
    assert mgr._is_startup_stalled(info, NOW) is False
    info._pid = 4242
    assert mgr._is_startup_stalled(info, NOW) is False


def test_unarmed_watcher_uses_the_built_in_budgets(monkeypatch):
    _budget(monkeypatch, None)
    assert _manager()._startup_deadline == _one_clock(90)


def test_lowering_the_budget_does_not_cut_a_start_short(monkeypatch):
    """A start that began under a 900 s budget keeps its window after a 90 s write."""
    _budget(monkeypatch, 900)
    mgr = _manager()
    info = _starting(NOW, 0)
    assert mgr._is_startup_stalled(info, NOW) is False
    _budget(monkeypatch, 90)
    assert mgr._is_startup_stalled(info, NOW + 1_000) is False


def test_a_new_start_clock_takes_the_current_budget(monkeypatch):
    """A lowered budget governs the next start clock (gate exit restarts it)."""
    _budget(monkeypatch, 900)
    mgr = _manager()
    info = _starting(NOW, 0)
    assert mgr._is_startup_stalled(info, NOW) is False
    _budget(monkeypatch, 90)
    info._exec_started = NOW + 1_000
    assert mgr._is_startup_stalled(info, NOW + 1_000 + _one_clock(90) + 1) is True


def test_explicit_startup_timeout_pins_the_window(monkeypatch):
    _budget(monkeypatch, 900)
    mgr = _manager(startup_timeout=120)
    assert mgr._startup_deadline == 120
    assert mgr._is_startup_stalled(_starting(NOW, 121), NOW) is True
