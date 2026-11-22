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
from kiro_crew.subagent_persistence import read_tombstone, write_tombstone
from kiro_crew.taskq import model
from kiro_crew.taskq.reconcile import reconcile_on_boot
from kiro_crew.taskq.store import TaskStore, TaskStoreUnavailable
from kiro_crew.testing.wait import await_until
from kiro_crew.testing.wait import finished as _finished

pytestmark = pytest.mark.usefixtures("healthy_host_memory", "close_subagent_managers")


@pytest.fixture
def quiet():
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        yield


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@pytest.mark.parametrize("claimer", ["never_settles", "releases_on_cancel", "releases_its_start"])
@pytest.mark.parametrize("earlier", ["no_tombstone", "restart_tombstone", "failed_attempt"])
async def test_a_claimed_row_left_by_a_teardown_is_cancelled_by_the_next_boot(
    quiet, earlier: str, claimer: str
) -> None:
    """The teardown leaves a claimed row to its claimer and records the parent end on
    the run folder. A restart before the claimer settles the row loses the in-memory
    teardown mark, and the boot reconcile reads the tombstone and cancels the row. The
    boot reconcile would otherwise requeue the ``admitted`` row, and the next pump pass
    would start it into the conversation that ended.

    * ``restart_tombstone``: the folder already holds the tombstone of the restart that
      stopped the row's earlier run, as a recovered row's folder does.
    * ``failed_attempt``: the row's earlier claim failed into ``retry_wait``, and the
      folder keeps that attempt's ``failed`` tombstone.
    * ``releases_on_cancel``: the shutdown cancels the claimer before its start, and
      the claimer gives the claim back to ``queued`` (``_release_untaken_claim``, the
      write ``_claim_or_release_on_cancel`` posts). The queued pass then reads the
      record, and the row is cancelled, not claimed and started again.
    * ``releases_its_start``: the claimer is cancelled after its start write and gives
      the start back to ``retry_wait`` (``_release_taken_start``). The same record
      cancels it.

    Neither earlier tombstone names the claim the parent end belongs to, so the record
    replaces it."""
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
    if earlier == "restart_tombstone":
        write_tombstone(agent_id, cause="gateway_restart", recovery_action="delivered")
    elif earlier == "failed_attempt":
        first = await store.run(store.claim, agent_id)
        assert first is not None
        assert await store.run(
            store.transition, agent_id, model.STARTING, generation=first.generation
        )
        assert await store.run(
            store.transition,
            agent_id,
            model.RETRY_WAIT,
            generation=first.generation,
            next_run_at=store.now() - 1,
        )
        write_tombstone(
            agent_id,
            cause="error",
            recovery_action="none",
            outcome="failed",
            taskq_attempt=first.generation,
        )
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
        claim = store.get(agent_id)
        assert claim is not None and tombstone["taskq_attempt"] == claim.generation
        if claimer == "releases_on_cancel":
            assert await store.run(
                SpawnAdmissionCoordinator._release_untaken_claim,
                store,
                agent_id,
                claim.generation,
            )
            assert store.state_of(agent_id) == model.QUEUED
        elif claimer == "releases_its_start":
            assert await store.run(
                store.transition, agent_id, model.STARTING, generation=claim.generation
            )
            assert await store.run(
                SpawnAdmissionCoordinator._release_taken_start,
                store,
                agent_id,
                claim.generation,
            )
            assert store.state_of(agent_id) == model.RETRY_WAIT

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


def test_the_parent_end_record_never_replaces_the_tombstone_of_its_own_attempt() -> None:
    """The create is exclusive: a run that ended on its own keeps its own record. Given
    an attempt, a tombstone that names that attempt is the run's own."""
    from kiro_crew.subagent_persistence import write_tombstone_if_absent

    write_tombstone("run-ended-first", cause="timeout", recovery_action="none", taskq_attempt=2)
    assert not write_tombstone_if_absent(
        "run-ended-first", cause="parent_end", recovery_action="none", attempt=2
    )
    tombstone = read_tombstone("run-ended-first")
    assert tombstone is not None and tombstone["cause"] == "timeout"

    write_tombstone("run-ended-unnamed", cause="timeout", recovery_action="none")
    assert not write_tombstone_if_absent(
        "run-ended-unnamed", cause="parent_end", recovery_action="none"
    )
    unnamed = read_tombstone("run-ended-unnamed")
    assert unnamed is not None and unnamed["cause"] == "timeout"

    assert write_tombstone_if_absent(
        "run-never-started", cause="parent_end", recovery_action="none", attempt=1
    )
    fresh = read_tombstone("run-never-started")
    assert fresh is not None and fresh["cause"] == "parent_end"
    assert fresh["taskq_attempt"] == 1


@pytest.mark.parametrize(
    "earlier",
    [
        {"cause": "gateway_restart", "recovery_action": "delivered", "leaked_process": True},
        {"cause": "gateway_restart", "recovery_action": "delivered", "outcome": "completed"},
        {"cause": "error", "recovery_action": "none", "outcome": "failed", "taskq_attempt": 1},
    ],
    ids=["restart", "restart_with_an_outcome", "failed_attempt"],
)
def test_a_parent_end_record_replaces_the_tombstone_of_an_earlier_attempt(
    earlier: dict[str, object],
) -> None:
    """The run folder is the row id's, so a row claimed again keeps the tombstone of its
    earlier attempt: the restart that stopped a recovered row's run, or the failure before
    a retry. Neither names the claim the parent end belongs to, so the record replaces it
    with its own ending and attempt, and keeps a ``leaked_process`` mark."""
    from kiro_crew.subagent_manager.admission.types import tombstone_terminal_state
    from kiro_crew.subagent_persistence import write_tombstone_if_absent

    write_tombstone("run-claimed-again", **earlier)  # type: ignore[arg-type]
    assert write_tombstone_if_absent(
        "run-claimed-again",
        cause="parent_end",
        recovery_action="none",
        attempt=2,
        outcome="stopped",
    )
    replaced = read_tombstone("run-claimed-again")
    assert replaced is not None and replaced["cause"] == "parent_end"
    assert (replaced["outcome"], replaced["taskq_attempt"]) == ("stopped", 2)
    assert replaced.get("leaked_process") is earlier.get("leaked_process")
    assert tombstone_terminal_state(replaced["cause"], replaced["outcome"]) == model.CANCELLED


def test_a_restart_tombstone_written_over_a_parent_end_keeps_the_parent_end() -> None:
    """A restart's bookkeeping write that lands after the parent end, such as the owed
    orphan notice's drain, records no ending, so the record keeps its ``parent_end``
    cause. A write that names an outcome is an ending and replaces it."""
    from kiro_crew.subagent_persistence import write_tombstone_if_absent

    assert write_tombstone_if_absent(
        "run-noticed-late",
        cause="parent_end",
        recovery_action="none",
        attempt=1,
        outcome="stopped",
        stop_origin="parent conversation ended (close)",
    )
    write_tombstone("run-noticed-late", cause="gateway_restart", recovery_action="delivered")
    kept = read_tombstone("run-noticed-late")
    assert kept is not None and kept["cause"] == "parent_end"
    assert kept["recovery_action"] == "delivered"
    assert kept.get("stop_origin") == "parent conversation ended (close)"
    assert (kept["outcome"], kept["taskq_attempt"]) == ("stopped", 1)

    write_tombstone(
        "run-noticed-late",
        cause="gateway_restart",
        recovery_action="delivered",
        outcome="completed",
    )
    ended = read_tombstone("run-noticed-late")
    assert ended is not None and ended["cause"] == "gateway_restart"


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
            claim = store.get(agent_id)
            assert claim is not None and tombstone["taskq_attempt"] == claim.generation

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


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_parent_end_tombstone_outlives_the_prune_window_until_its_row_is_terminal(
    quiet, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reaper's tombstone pruner keeps an aged ``parent_end`` tombstone while its row is
    still claimed. Pruned on its window, the record would be gone before the store came
    back, and with the teardown mark aged out too the retained-claim retry would start the
    row. Once the row is terminal the tombstone is pruned like any other."""
    import json
    import time

    from kiro_crew.subagent_persistence import prune_stale_tombstones, write_tombstone_if_absent

    monkeypatch.setattr("kiro_crew.subagent_persistence._SUBAGENTS_DIR", tmp_path)
    mgr = await _manager(max_concurrent=1)
    try:
        store: TaskStore = mgr._taskq
        (rid,) = store.accept([model.TaskRecord(id="pe1", kind=model.KIND_SUBAGENT)])
        assert store.claim(rid) is not None
        assert write_tombstone_if_absent(rid, cause="parent_end", recovery_action="none")
        path = tmp_path / rid / "tombstone.json"
        aged = json.loads(path.read_text(encoding="utf-8"))
        aged["died"] = time.time() - 8 * 86400
        path.write_text(json.dumps(aged), encoding="utf-8")
        keep = mgr._monitor._refusal_tombstone_unsettled

        assert prune_stale_tombstones(7, 3600, keep) == 0
        assert (read_tombstone(rid) or {}).get("cause") == "parent_end"
        assert store.cancel(rid, reason="parent_end") is not None
        assert prune_stale_tombstones(7, 3600, keep) == 1
        assert read_tombstone(rid) is None
    finally:
        await _finished(mgr.cancel_all(), "the manager cleanup")


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_failed_parent_end_write_is_logged_and_the_teardown_goes_on(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A tombstone write that fails does not stop the teardown: it is logged, and the
    in-memory teardown mark stays the only record, as it is without the write."""
    import logging

    from kiro_crew import subagent_persistence
    from kiro_crew.subagent_manager.parent_end_record import record_parent_end

    def _unwritable(*_args: object, **_kwargs: object) -> bool:
        raise OSError("run folder unwritable")

    monkeypatch.setattr(subagent_persistence, "write_tombstone_if_absent", _unwritable)
    with caplog.at_level(logging.WARNING, logger="kiro_crew.subagent"):
        await record_parent_end("run-unwritable", _STOP_PARENT, "close", attempt=1)

    assert any("run-unwritable" in r.getMessage() for r in caplog.records)
    assert read_tombstone("run-unwritable") is None


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_parent_end_record_refused_by_the_runs_own_tombstone_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A record the run's own tombstone refuses is not silent: the log names the row and
    the attempt, and the run's tombstone stays as it was."""
    import logging

    from kiro_crew.subagent_manager.parent_end_record import record_parent_end

    write_tombstone("run-own-ending", cause="timeout", recovery_action="none", taskq_attempt=4)
    with caplog.at_level(logging.INFO, logger="kiro_crew.subagent"):
        await record_parent_end("run-own-ending", _STOP_PARENT, "close", attempt=4)

    assert any(
        "run-own-ending" in r.getMessage() and "attempt 4" in r.getMessage() for r in caplog.records
    )
    kept = read_tombstone("run-own-ending")
    assert kept is not None and kept["cause"] == "timeout"


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_claimer_that_finds_the_teardowns_record_logs_no_refusal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The teardown records the parent end of attempt 4. The claimer's cancel then
    cannot reach the store, so it records the same parent end, and again on each
    retained-claim retry. The record is on disk, so no line says it is not recorded,
    while a run whose own ending refuses the record still gets that line."""
    import logging

    from kiro_crew.subagent_manager.parent_end_record import leave_to_claimer, record_parent_end
    from kiro_crew.subagent_persistence import write_tombstone_if_absent

    claimer_origin = "parent conversation ended; the claimer's cancel could not reach the store"
    await leave_to_claimer("run-left-to-claimer", _STOP_PARENT, "close", attempt=4)
    write_tombstone("run-own-ending-too", cause="timeout", recovery_action="none", taskq_attempt=4)
    with caplog.at_level(logging.INFO, logger="kiro_crew.subagent"):
        for _retry in range(2):
            await record_parent_end(
                "run-left-to-claimer", _STOP_PARENT, attempt=4, stop_origin=claimer_origin
            )
        await record_parent_end(
            "run-own-ending-too", _STOP_PARENT, attempt=4, stop_origin=claimer_origin
        )

    refused = [r.getMessage() for r in caplog.records if "not recorded" in r.getMessage()]
    assert len(refused) == 1 and "run-own-ending-too" in refused[0]
    assert write_tombstone_if_absent(
        "run-left-to-claimer", cause="parent_end", recovery_action="none", attempt=4
    )
    kept = read_tombstone("run-left-to-claimer")
    assert kept is not None and kept["cause"] == "parent_end" and kept["taskq_attempt"] == 4
