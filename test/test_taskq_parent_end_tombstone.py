"""A parent end that leaves a claimed row to its claimer survives a gateway restart.

The real admission coordinator and a real ``TaskStore`` run with no worker. The row is
claimed in the store, the parent-end teardown leaves it to its claimer, and a second
``TaskStore`` on the same file stands in for the next process, whose boot reconcile
decides what happens to the row.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from test_taskq_admission_integration import _STOP_PARENT, _manager, _store_path

from kiro_crew.subagent import SubagentManager
from kiro_crew.subagent_manager.admission import SpawnAdmissionCoordinator
from kiro_crew.subagent_persistence import read_tombstone
from kiro_crew.taskq import model
from kiro_crew.taskq.reconcile import reconcile_on_boot
from kiro_crew.taskq.store import TaskStore
from kiro_crew.testing.wait import finished as _finished

pytestmark = pytest.mark.usefixtures("healthy_host_memory")


@pytest.fixture
def quiet():
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        yield


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_claimed_row_left_by_a_teardown_is_cancelled_by_the_next_boot(quiet) -> None:
    """The teardown leaves a claimed row to its claimer and records the parent end on
    the run folder. A restart before the claimer settles the row loses the in-memory
    teardown mark, and the boot reconcile reads the tombstone and cancels the row. The
    boot reconcile would otherwise requeue the ``admitted`` row, and the next pump pass
    would start it into the conversation that ended."""
    mgr = await _manager(max_concurrent=1)
    with patch.object(SubagentManager, "_run", new=AsyncMock()):
        # Another parent's run takes the one slot, so this parent's row queues.
        mgr.spawn("occupy", parent_session_key="dash:elsewhere")
        waiting = mgr.spawn("waiting", parent_session_key=_STOP_PARENT)
    assert waiting.queued and not waiting.done
    agent_id = waiting.id
    store: TaskStore = mgr._taskq
    # The pump pops a row from the window before it claims it.
    params = mgr._queue.pop(0)
    assert params["_preassigned_id"] == agent_id
    try:
        # The claim the pump takes before its start write, with no claimer behind it:
        # the process ends before that claimer gets to the row.
        assert await store.run(store.claim, agent_id) is not None
        assert store.state_of(agent_id) == model.ADMITTED

        await mgr.cancel_for_teardown([agent_id], parent_session_key=_STOP_PARENT, verb="close")

        assert store.state_of(agent_id) == model.ADMITTED, "the teardown left it to its claimer"
        tombstone = read_tombstone(agent_id)
        assert tombstone is not None and tombstone["cause"] == "parent_end"
        assert tombstone["parent_session"] == _STOP_PARENT

        successor = TaskStore(_store_path(), network_fs=False).open()
        try:
            report = reconcile_on_boot(
                successor, artifact_probe=SpawnAdmissionCoordinator.taskq_artifact_probe
            )
            assert report.errors == []
            assert successor.state_of(agent_id) == model.CANCELLED
            assert report.requeued == 0
        finally:
            successor.close()
    finally:
        await _finished(mgr.cancel_all(), "the manager cleanup")


def test_the_parent_end_record_never_replaces_a_tombstone_the_run_wrote() -> None:
    """The create is exclusive: a run that ended on its own keeps its own record."""
    from kiro_crew.subagent_persistence import write_tombstone, write_tombstone_if_absent

    write_tombstone("run-ended-first", cause="timeout", recovery_action="none")

    assert not write_tombstone_if_absent(
        "run-ended-first", cause="parent_end", recovery_action="none"
    )
    tombstone = read_tombstone("run-ended-first")
    assert tombstone is not None and tombstone["cause"] == "timeout"

    assert write_tombstone_if_absent(
        "run-never-started", cause="parent_end", recovery_action="none"
    )
    fresh = read_tombstone("run-never-started")
    assert fresh is not None and fresh["cause"] == "parent_end"
