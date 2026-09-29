"""A halted autopilot stage cancels its children through a REAL SubagentManager.

The dashboard-side tests pin the call shape with mock managers. This one drives
``_cancel_exhausted_stage_subagents`` against a real ``SubagentManager`` on top
of the durable task queue, so the reservation, the held boundary cancel and the
queue settle all actually run. The one faked seam is the run itself (no
kiro-cli) and the reap (no provider to tear down).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_test_helpers import _make_state

from kiro_crew.subagent import SubagentManager
from kiro_crew.taskq import model

pytestmark = pytest.mark.usefixtures("healthy_host_memory")


@pytest.fixture(autouse=True)
def _isolate_config_dir(tmp_path, monkeypatch):
    for module in ("state", "chat", "chat_orchestrator"):
        monkeypatch.setattr(f"kiro_crew.dashboard.{module}.config_dir", lambda: tmp_path)


@pytest.fixture
def quiet():
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        yield


def _sessions() -> MagicMock:
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    sessions.get_approval_policy = MagicMock(return_value="auto")
    sessions.get_agent = MagicMock(return_value="")
    sessions.get_agent_selection = MagicMock(return_value=("template", ""))
    sessions.has_session = MagicMock(return_value=True)
    sessions.release = MagicMock()
    sessions.reset = AsyncMock()
    return sessions


@asynccontextmanager
async def _manager() -> AsyncIterator[SubagentManager]:
    ctx = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = True
    mgr = SubagentManager(sessions=_sessions(), ctx_builder=ctx, max_concurrent=1)
    try:
        await mgr.wait_taskq_ready()
        mgr._spawn_stagger_secs = 0.0
        mgr._last_spawn_ts = 0.0
        mgr._taskq._window = 1
        yield mgr
    finally:
        try:
            await mgr.cancel_all()
        finally:
            mgr.close()


@pytest.mark.asyncio
async def test_halt_cancels_running_and_queued_stage_children_only(tmp_path, quiet):
    from kiro_crew.dashboard.chat_orchestrator import _cancel_exhausted_stage_subagents

    async with _manager() as mgr:
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("halt-real-manager", mode="orchestrator")
        slot.stage_boundary.arm(1, consumed=False)
        owner = slot.stage_boundary.owner
        assert owner
        parent = f"dashboard:{slot.key}"

        with patch.object(SubagentManager, "_run", new=AsyncMock()):
            running = mgr.spawn(
                "stage work", parent_session_key=parent, _stage_boundary_owner=owner
            )
            queued = mgr.spawn(
                "queued stage work", parent_session_key=parent, _stage_boundary_owner=owner
            )
            foreign = mgr.spawn(
                "another stage's work",
                parent_session_key=parent,
                _stage_boundary_owner="some-other-owner",
            )
        assert mgr._taskq.state_of(queued.id) == model.QUEUED
        assert mgr._taskq.state_of(foreign.id) == model.QUEUED
        mgr._report_queued_stop = MagicMock()  # type: ignore[method-assign]
        mgr._force_reap = AsyncMock()  # type: ignore[method-assign]
        state.subagents = mgr

        outcome = await _cancel_exhausted_stage_subagents(state, slot)

        assert outcome.attempted is True
        assert outcome.failed == 0 and outcome.pending == () and outcome.refused == ()
        # The queued child never starts: its durable row is cancelled.
        assert mgr._taskq.state_of(queued.id) == model.CANCELLED
        # The live child was revoked and sent down the reap path.
        assert running._stage_boundary_cancelled is True
        reaped = {call.args[0] for call in mgr._force_reap.await_args_list}
        assert running.id in reaped
        assert outcome.stopped >= 2
        # Another owner's work under the SAME parent is untouched.
        assert mgr._taskq.state_of(foreign.id) == model.QUEUED
        assert foreign.id not in reaped
        # A settled cancel leaves no hold behind, so Go can still resume the plan.
        assert (parent, owner) not in mgr._pending_boundary_cancellations

    assert mgr._taskq is None


@pytest.mark.asyncio
async def test_halt_blocks_queued_stage_rows_while_the_store_cancel_is_suspended(tmp_path, quiet):
    """The hold is what keeps a queued stage row from dispatching mid-cancel.

    ``taskq_cancel_boundary_async`` suspends on the store write. While it is
    suspended, the dispatcher's own gate (``_boundary_cancellation_pending``)
    must refuse this stage's queued rows -- otherwise a pump pass in that gap
    starts the row and it runs to its own deadline. Without the reservation
    and the retained hold, the gate reads False here and this test fails.
    """
    from kiro_crew.dashboard.chat_orchestrator import _cancel_exhausted_stage_subagents

    async with _manager() as mgr:
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("halt-real-hold", mode="orchestrator")
        slot.stage_boundary.arm(1, consumed=False)
        owner = slot.stage_boundary.owner
        parent = f"dashboard:{slot.key}"

        with patch.object(SubagentManager, "_run", new=AsyncMock()):
            mgr.spawn("stage work", parent_session_key=parent, _stage_boundary_owner=owner)
            queued = mgr.spawn(
                "queued stage work", parent_session_key=parent, _stage_boundary_owner=owner
            )
        assert mgr._taskq.state_of(queued.id) == model.QUEUED
        mgr._report_queued_stop = MagicMock()  # type: ignore[method-assign]
        mgr._force_reap = AsyncMock()  # type: ignore[method-assign]
        state.subagents = mgr

        stage_row = {"parent_session_key": parent, "_stage_boundary_owner": owner}
        gate_during_write: list[bool] = []
        coordinator = type(mgr._admission)
        real_cancel = coordinator.taskq_cancel_boundary_async

        async def suspended_cancel(self, parent_key, boundary_owner):
            gate_during_write.append(mgr._boundary_cancellation_pending(stage_row))
            return await real_cancel(self, parent_key, boundary_owner)

        with patch.object(coordinator, "taskq_cancel_boundary_async", suspended_cancel):
            outcome = await _cancel_exhausted_stage_subagents(state, slot)

        assert gate_during_write == [True]
        assert mgr._taskq.state_of(queued.id) == model.CANCELLED
        assert outcome.pending == () and outcome.refused == ()
        # Settling released the hold, so Go is not blocked afterwards.
        assert mgr._boundary_cancellation_pending(stage_row) is False

    assert mgr._taskq is None
