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


@pytest.mark.asyncio
async def test_force_reap_approval_parked_reports_unanswered_spawn_approval():
    """An approval-parked run (never started) reaped by the wall clock must NOT
    be framed as an execution deadline it could not have reached.

    This exercises the default-reason wall-clock reaper path (no ``reason``),
    the exact path that produced the misleading
    ``Reaped after 1801s (exceeded 1800s deadline) [turn 0/100]`` message.
    """
    mgr = _make_manager(startup_timeout=120)
    _neuter_force_reap_collaborators(mgr)
    # turns==0, _pid is None, _exec_started is None: the run never began.
    info = _info(_awaiting_approval=True, _exec_started=None, _pid=None, turns=0)

    await mgr._force_reap("a1b2c3d4", info, 1801.0)

    assert info.done is True
    # Accurate cause: never-answered spawn approval, run never started.
    assert "awaiting" in info.error
    assert "spawn approval" in info.error
    assert "never started" in info.error
    # Must NOT blame an execution deadline the run could not have reached.
    assert "exceeded" not in info.error
    assert "deadline" not in info.error
    # Tombstone reason string is unchanged.
    assert mgr._write_tombstone.call_args.args[1] == "reaped"


@pytest.mark.asyncio
async def test_force_reap_midrun_tool_parked_keeps_deadline_message():
    """Proves the ``_exec_started is None`` conjunct is load-bearing.

    ``run.py`` sets ``_awaiting_approval`` for mid-run TOOL prompts too, but
    those runs already have ``_exec_started`` set. Such a run WAS running, so a
    reap must keep the generic deadline message and NOT be misreported as an
    unanswered spawn approval.
    """
    mgr = _make_manager(startup_timeout=120)
    _neuter_force_reap_collaborators(mgr)
    # _awaiting_approval True but execution already started (mid-run tool prompt).
    info = _info(_awaiting_approval=True, _exec_started=1.0, _pid=None, turns=1)

    await mgr._force_reap("a1b2c3d4", info, 1801.0)

    assert info.done is True
    assert "Reaped after 1801s" in info.error
    assert "exceeded" in info.error
    assert "deadline" in info.error
    # Did NOT enter the new approval-parked branch.
    assert "spawn approval" not in info.error
    assert mgr._write_tombstone.call_args.args[1] == "reaped"


# ── production wiring: the spawn-approval gate sets _awaiting_approval ──


@pytest.mark.asyncio
async def test_spawn_with_approval_marks_awaiting_flag_while_parked():
    """The pre-execution spawn gate must set ``_awaiting_approval`` for the
    duration of the human approval wait.

    This is the production wire that ``_force_reap``'s approval-parked branch
    depends on: without it, a run reaped while parked on an unanswered spawn
    approval has ``_awaiting_approval`` False and falls through to the generic
    deadline message — the exact bug this task fixes. The unit tests above
    hand-set the flag, so only this test catches a regression that stops the
    gate from setting it. It also pins ``_exec_started is None`` at the wait,
    which is what makes the reap branch distinguish "never started" from a
    mid-run tool prompt.
    """
    mgr = _make_manager(startup_timeout=120)

    seen: dict[str, object] = {}

    async def _approve(request_id, description, session_key):  # noqa: ANN001
        # Snapshot the flag/exec state at the moment the run is parked on the
        # approval — this is exactly the window in which the reaper fires.
        seen["awaiting"] = info._awaiting_approval
        seen["exec_started"] = info._exec_started
        seen["description"] = description
        return True

    mgr._on_spawn_approval = _approve
    # Neuter everything the approved path would do AFTER the wait — we only
    # care that the flag is set during the await and cleared after it.
    mgr._log_spawned = MagicMock()
    mgr._run = AsyncMock()

    info = _info(task="do a thing", parent_session_key="sess-1")
    assert info._awaiting_approval is False

    await mgr._spawn_with_approval(info)

    # During the human wait the run was flagged as awaiting approval and had
    # NOT begun execution.
    assert seen["awaiting"] is True
    assert seen["exec_started"] is None
    # The spawn_run(...) preview reached the approval callback: a quick check that
    # the flag was observed on the real wire, not on a stubbed-out one.
    assert "spawn_run(" in str(seen["description"])
    # The flag is a bounded human-wait window: cleared once the wait resolves,
    # regardless of outcome (finally), so it never leaks into execution.
    assert info._awaiting_approval is False
    mgr._run.assert_awaited_once()
