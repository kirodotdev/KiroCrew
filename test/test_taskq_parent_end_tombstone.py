"""A parent end that leaves a claimed row to its claimer survives a gateway restart.

The real admission coordinator and a real ``TaskStore`` run with no worker. The row is
claimed in the store, the parent-end teardown leaves it to its claimer, and a second
``TaskStore`` on the same file stands in for the next process, whose boot reconcile
decides what happens to the row.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from overload_fakes import settle_store_writes
from test_taskq_admission_integration import _STOP_PARENT, _manager, _store_path
from test_taskq_claim_start import _park_run, _popped_row

from kiro_crew.subagent import SubagentManager
from kiro_crew.subagent_manager.admission import SpawnAdmissionCoordinator
from kiro_crew.subagent_persistence import read_tombstone
from kiro_crew.taskq import model
from kiro_crew.taskq.reconcile import reconcile_on_boot
from kiro_crew.taskq.store import TaskStore, TaskStoreUnavailable
from kiro_crew.testing.wait import await_until
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


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@pytest.mark.parametrize("then", ["mark_expires", "gateway_restarts"])
async def test_a_retained_parent_end_claim_stays_unstarted_without_the_teardown_mark(
    quiet, monkeypatch: pytest.MonkeyPatch, then: str
) -> None:
    """The teardown lands while the claimer's start write is in flight, so it finds the
    row ``starting`` and writes no tombstone. The claimer reads the mark, and its cancel
    hits a store outage, so it retains the claim and records the parent end on the run
    folder itself. That record is what holds once the in-memory mark is gone:

    * ``mark_expires``: the mark ages out before the retained-claim retry, and the
      retry still cancels the row instead of starting it.
    * ``gateway_restarts``: the next process's boot reconcile cancels the row instead of
      recovering it."""
    mgr, params = await _popped_row(monkeypatch)
    store: TaskStore = mgr._taskq
    agent_id = params["_preassigned_id"]
    real_run = store.run
    taskq_start = mgr._admission.taskq_claim_start
    started, resume = asyncio.Event(), asyncio.Event()
    outages = 0

    async def _gated(fn, /, *args, **kwargs):
        nonlocal outages
        if fn == store.cancel and kwargs.get("reason") == "parent_end" and not outages:
            outages += 1
            raise TaskStoreUnavailable("store down under the parent-end cancel")
        result = await real_run(fn, *args, **kwargs)
        if fn == taskq_start and not started.is_set():
            started.set()
            await await_until(resume, what="the test releasing the start write")
        return result

    try:
        with (
            patch.object(store, "run", side_effect=_gated),
            patch.object(SubagentManager, "_run", new=_park_run),
        ):
            dispatch = asyncio.create_task(mgr._admission._dispatch_async_impl(params))
            await await_until(started)
            await await_until(
                asyncio.create_task(
                    mgr.cancel_for_teardown(
                        [agent_id], parent_session_key=_STOP_PARENT, verb="close"
                    )
                ),
                what="the teardown, which must not wait on the gated claimer",
            )
            assert read_tombstone(agent_id) is None, "the teardown found the row starting"
            resume.set()
            await await_until(dispatch)
            assert outages == 1
            assert agent_id in mgr._retained_claims
            assert store.state_of(agent_id) == model.STARTING
            tombstone = read_tombstone(agent_id)
            assert tombstone is not None and tombstone["cause"] == "parent_end"

            if then == "mark_expires":
                mgr._teardown_cancelled_ids.discard(agent_id)
                await _finished(mgr._drain_queue_pass(), "the retained claim retry")
                await settle_store_writes(store, rounds=4)
                assert agent_id not in mgr._tasks, "a torn-down claimed row never starts"
                assert agent_id not in mgr._retained_claims
                assert mgr._running_count == 0 and mgr._startup_reservations == 0
                assert store.state_of(agent_id) == model.CANCELLED
            else:
                successor = TaskStore(_store_path(), network_fs=False).open()
                try:
                    report = reconcile_on_boot(
                        successor, artifact_probe=SpawnAdmissionCoordinator.taskq_artifact_probe
                    )
                    assert report.errors == []
                    assert successor.state_of(agent_id) == model.CANCELLED
                finally:
                    successor.close()
    finally:
        resume.set()
        await _finished(mgr.cancel_all(), "the manager cleanup")


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_only_a_readable_parent_end_tombstone_counts_as_a_recorded_parent_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The retained-claim retry trusts a ``parent_end`` tombstone and nothing else: another
    cause, no tombstone, or a folder it cannot read leaves the teardown mark as the only
    record."""
    from kiro_crew import subagent_persistence
    from kiro_crew.subagent_persistence import write_tombstone_if_absent

    recorded = SpawnAdmissionCoordinator._parent_end_recorded
    assert write_tombstone_if_absent("run-parent-ended", cause="parent_end", recovery_action="none")
    assert write_tombstone_if_absent("run-timed-out", cause="timeout", recovery_action="none")

    assert await recorded(None, "run-parent-ended")  # type: ignore[arg-type]
    assert not await recorded(None, "run-timed-out")  # type: ignore[arg-type]
    assert not await recorded(None, "run-never-recorded")  # type: ignore[arg-type]

    def _unreadable(_agent_id: str) -> None:
        raise OSError("run folder unreadable")

    monkeypatch.setattr(subagent_persistence, "read_tombstone", _unreadable)
    assert not await recorded(None, "run-parent-ended")  # type: ignore[arg-type]


class _GatedCancelStore:
    """A store whose parent-end cancel waits on the test, then answers or fails."""

    def __init__(self, *, fails: bool) -> None:
        self.fails = fails
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    def cancel(self, *_args: object, **_kwargs: object) -> str:
        return model.ADMITTED

    async def run(self, fn, /, *args, **kwargs):
        self.entered.set()
        await await_until(self.release, what="the test releasing the cancel")
        if self.fails:
            raise TaskStoreUnavailable("store down under the parent-end cancel")
        return fn(*args, **kwargs)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@pytest.mark.parametrize("fails", [False, True], ids=["cancel_lands", "cancel_fails"])
async def test_a_claimer_cancelled_under_its_parent_end_cancel_still_propagates_its_cancel(
    fails: bool,
) -> None:
    """The claimer's cancel write is shielded and awaited to its answer, and the claimer's
    own cancel still propagates afterwards, also when that write hits a store outage. A
    swallowed cancel would let a shutting-down claimer carry on as if nothing happened."""
    store = _GatedCancelStore(fails=fails)
    claimer = asyncio.create_task(
        SpawnAdmissionCoordinator._cancel_torn_down_claim(
            None, store, "run-claimed", 3  # type: ignore[arg-type]
        )
    )
    await await_until(store.entered)
    claimer.cancel()
    store.release.set()
    with pytest.raises(asyncio.CancelledError):
        await await_until(claimer, what="the cancelled claimer")
    assert claimer.cancelled()
