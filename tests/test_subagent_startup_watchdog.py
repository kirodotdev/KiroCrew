"""Tests for the subagent startup watchdog (subagent.py).

Covers ``SubagentManager._is_startup_stalled`` (which a wedged, never-started
subagent trips) and ``_force_reap(reason="startup_timeout")`` (the clear
"failed to start" error + tombstone cause it produces).

Regression target: a subagent whose ``_run_inner`` wedged before launching its
runtime (no pid) and before its first turn used to sit for the full 1800s
deadline and then surface a misleading "Reaped after 1800s [turn 0/100]"
error. The startup watchdog now reaps it after a short window with an accurate
"Failed to start" message, while never touching an agent that is merely
awaiting spawn approval.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.subagent import SubagentInfo, SubagentManager


def _make_manager(startup_timeout: int = 120) -> SubagentManager:
    return SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        startup_timeout=startup_timeout,
    )


def _info(**overrides) -> SubagentInfo:
    info = SubagentInfo(id="a1b2c3d4", task="t", agent="")
    for k, v in overrides.items():
        setattr(info, k, v)
    return info


# ── _is_startup_stalled ──────────────────────────────────────────────


def test_stalled_true_when_execution_started_but_no_runtime_or_turn():
    mgr = _make_manager(startup_timeout=120)
    now = 1_000.0
    # entered _run_inner 200s ago, never launched a runtime, never took a turn
    info = _info(_exec_started=now - 200, _pid=None, turns=0)
    assert mgr._is_startup_stalled(info, now) is True


def test_stalled_false_when_awaiting_approval():
    """An agent awaiting spawn approval never entered _run_inner.

    ``_exec_started`` is None, so it must NEVER trip the watchdog even though
    it has been registered (``started``) far longer than the window.
    """
    mgr = _make_manager(startup_timeout=120)
    now = 1_000.0
    info = _info(started=now - 5_000, _exec_started=None, _pid=None, turns=0)
    assert mgr._is_startup_stalled(info, now) is False


def test_stalled_false_when_runtime_launched():
    mgr = _make_manager(startup_timeout=120)
    now = 1_000.0
    # runtime pid assigned -> past startup; a slow first turn must not be reaped
    info = _info(_exec_started=now - 600, _pid=4242, turns=0)
    assert mgr._is_startup_stalled(info, now) is False


def test_stalled_false_when_a_turn_was_produced():
    mgr = _make_manager(startup_timeout=120)
    now = 1_000.0
    info = _info(_exec_started=now - 600, _pid=None, turns=1)
    assert mgr._is_startup_stalled(info, now) is False


def test_stalled_false_within_startup_window():
    mgr = _make_manager(startup_timeout=120)
    now = 1_000.0
    info = _info(_exec_started=now - 30, _pid=None, turns=0)
    assert mgr._is_startup_stalled(info, now) is False


# ── _force_reap(reason="startup_timeout") ────────────────────────────


def _neuter_force_reap_collaborators(mgr: SubagentManager) -> None:
    mgr._sessions.reset = AsyncMock()
    mgr._sigkill_session = MagicMock()
    mgr._write_tombstone = MagicMock()
    mgr._record_cost = MagicMock()
    mgr._drain_queue = MagicMock()
    mgr._fire_event = AsyncMock()


@pytest.mark.asyncio
async def test_force_reap_startup_timeout_error_and_tombstone_cause():
    mgr = _make_manager(startup_timeout=120)
    _neuter_force_reap_collaborators(mgr)
    info = _info(_exec_started=1.0, _pid=None, turns=0)

    await mgr._force_reap("a1b2c3d4", info, 130.0, reason="startup_timeout")

    assert info.done is True
    assert "Failed to start within 120s" in info.error
    assert "exceeded" not in info.error  # not the generic deadline message
    mgr._write_tombstone.assert_called_once()
    assert mgr._write_tombstone.call_args.args[1] == "startup_timeout"


@pytest.mark.asyncio
async def test_force_reap_default_reason_keeps_deadline_message():
    """Regression: the default (no-reason) reap message/cause are unchanged."""
    mgr = _make_manager(startup_timeout=120)
    _neuter_force_reap_collaborators(mgr)
    info = _info(_exec_started=1.0, _pid=None, turns=0)

    await mgr._force_reap("a1b2c3d4", info, 1810.0)

    assert info.done is True
    assert "Reaped after 1810s" in info.error
    assert mgr._write_tombstone.call_args.args[1] == "reaped"
