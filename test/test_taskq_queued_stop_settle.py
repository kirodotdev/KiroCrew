"""A stop of a queued run is published only once the row is durably ended.

The Stop drops the run's window entry and registers its stopped record at once,
which is what the claimer refuses. When the row's store cancel does not land (a
contended store raising ``TaskStoreUnavailable``) the stop is not told to the
caller on that alone: the row's queued-stop marker goes to disk first, the
cancel is retried until it lands, and the report and ``cancel``'s answer wait for
it. Past the read bound, the marker confirmed on disk is the verdict instead:
the store holds the row out of every claim and the next boot settles it
``cancelled``. Stop all waits only that bound and counts only the rows whose
hold released; the parent-end teardown holds its report the same way.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from queue_depth_reconcile_helpers import _PARENT, _close, _manager, _park, _record, _settle

import kiro_crew.subagent_manager.admission.taskq_bridge as bridge_mod
from kiro_crew.subagent import SubagentManager
from kiro_crew.subagent_persistence import (
    create_agent_folder,
    prune_stale_tombstones,
    read_tombstone,
)
from kiro_crew.taskq import model
from kiro_crew.taskq.reconcile import reconcile_on_boot
from kiro_crew.taskq.store import TaskStore, TaskStoreUnavailable
from kiro_crew.testing.wait import DEFAULT_INTERVAL_SECS, async_wait_until, await_until

pytestmark = pytest.mark.usefixtures("healthy_host_memory")


def _contend(
    monkeypatch: pytest.MonkeyPatch,
    store: TaskStore,
    row_id: str,
    times: int,
    events: list | None = None,
    reported_at_landing: list | None = None,
) -> list[str]:
    """Refuse *row_id*'s next *times* cancels, and every ``finish`` of it, as a locked store does.

    The ``subagent_done`` ids already in *events* when a cancel of the row lands
    go to *reported_at_landing*.
    """
    refused: list[str] = []
    real_cancel = store.cancel
    real_finish = store.finish

    def cancel(task_id: str, **kw: Any) -> Any:
        if task_id == row_id and len(refused) < times:
            refused.append(task_id)
            raise TaskStoreUnavailable("database is locked")
        if task_id == row_id and reported_at_landing is not None:
            reported_at_landing.append([i for e, _k, i, _x in events or () if e == "subagent_done"])
        return real_cancel(task_id, **kw)

    def finish(task_id: str, *a: Any, **kw: Any) -> Any:
        if task_id == row_id:
            raise TaskStoreUnavailable("database is locked")
        return real_finish(task_id, *a, **kw)

    monkeypatch.setattr(store, "cancel", cancel)
    monkeypatch.setattr(store, "finish", finish)
    return refused


async def _queued_row(mgr: SubagentManager) -> str:
    with patch.object(SubagentManager, "_run", new=_park):
        mgr.spawn("holds the slot", parent_session_key="dash:elsewhere")
        row = mgr.spawn(
            "work the user then stops", parent_session_key=_PARENT, _memory_mode="persistent"
        )
        await _settle(mgr)
    assert mgr._taskq.state_of(row.id) == model.QUEUED
    return row.id


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_queued_stop_whose_cancel_keeps_missing_is_answered_once_the_row_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first cancel and the next retry are refused: ``cancel`` answers, and the
    stop is reported, only after a later retry has cancelled the row."""
    monkeypatch.setattr(bridge_mod, "_SETTLE_RETRY_BACKOFF_CAP_SECS", DEFAULT_INTERVAL_SECS)
    mgr = await _manager(monkeypatch, pump_off_loop=True, max_concurrent=1)
    try:
        row_id = await _queued_row(mgr)
        store = mgr._taskq
        events = _record(mgr)
        reported_at_landing: list = []
        refused = _contend(
            monkeypatch,
            store,
            row_id,
            times=2,
            events=events,
            reported_at_landing=reported_at_landing,
        )

        with patch.object(SubagentManager, "_run", new=_park):
            assert await mgr.cancel(row_id) is True
            answered_over = store.state_of(row_id)
            await _settle(mgr)

        assert refused == [row_id, row_id]
        assert answered_over == model.CANCELLED, "the stop was answered over an uncancelled row"
        assert reported_at_landing[:1] == [[]], "the stop was reported before its cancel landed"
        assert [i for etype, _k, i, _x in events if etype == "subagent_done"] == [row_id]
        marker = read_tombstone(row_id) or {}
        # The marker's outcome is what the status read shows after a restart.
        assert (marker.get("cause"), marker.get("outcome")) == ("queued_stop", "stopped")
    finally:
        await mgr.cancel_all()
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_queued_stop_on_a_wedged_store_is_answered_on_its_marker_and_cancelled_at_boot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every cancel is refused: past the read bound the marker on disk releases the
    stop, the store holds the row out of every claim, and the next boot's
    reconcile, still refused its cancel, holds it for ``cancelled`` until that lands."""
    from kiro_crew.taskq.reconcile import STOPPED

    monkeypatch.setattr(bridge_mod, "_SETTLE_RETRY_BACKOFF_CAP_SECS", DEFAULT_INTERVAL_SECS)
    mgr = await _manager(monkeypatch, pump_off_loop=True, max_concurrent=1)
    try:
        row_id = await _queued_row(mgr)
        store = mgr._taskq
        _contend(monkeypatch, store, row_id, times=10**6)

        with patch.object(SubagentManager, "_run", new=_park):
            assert await mgr.cancel(row_id) is True

        assert store.state_of(row_id) == model.QUEUED
        assert (read_tombstone(row_id) or {}).get("cause") == "queued_stop"
        assert row_id in store.held_until_settled()
        rec = store.get(row_id)
        assert rec is not None
        assert mgr._admission.taskq_artifact_probe(rec) == STOPPED

        report = reconcile_on_boot(store, artifact_probe=mgr._admission.taskq_artifact_probe)
        assert report.held == 1, report
        assert store._release_settled_holds() == []
        assert store.state_of(row_id) == model.CANCELLED
    finally:
        await mgr.cancel_all()
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_shutdown_while_a_queued_stop_settles_keeps_its_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gateway stops while the stop's report still waits on its settle (its
    marker is on disk but not read back, so nothing releases the stop): the
    shutdown drain cancels that report, and the marker the next boot settles the
    row ``cancelled`` on stays on disk."""
    monkeypatch.setattr(bridge_mod, "_SETTLE_RETRY_BACKOFF_CAP_SECS", DEFAULT_INTERVAL_SECS)
    monkeypatch.setattr("kiro_crew.subagent._REPORT_DRAIN_TIMEOUT", 0.05)
    mgr = await _manager(monkeypatch, pump_off_loop=True, max_concurrent=1)
    stop: asyncio.Future | None = None
    try:
        row_id = await _queued_row(mgr)
        _contend(monkeypatch, mgr._taskq, row_id, times=10**6)
        real_marker = type(mgr._admission)._tombstone_queued_stop

        def _written_not_confirmed(agent_id: str) -> bool:
            real_marker(agent_id)
            return False

        monkeypatch.setattr(
            type(mgr._admission), "_tombstone_queued_stop", staticmethod(_written_not_confirmed)
        )

        with patch.object(SubagentManager, "_run", new=_park):
            stop = asyncio.ensure_future(mgr.cancel(row_id))
            await async_wait_until(
                lambda: (read_tombstone(row_id) or {}).get("cause") == "queued_stop"
            )
            await mgr.cancel_all()

        assert (read_tombstone(row_id) or {}).get("cause") == "queued_stop"
    finally:
        if stop is not None:
            stop.cancel()
            await asyncio.gather(stop, return_exceptions=True)
        _close(mgr)


@pytest.mark.asyncio
async def test_a_stale_queued_stop_tombstone_is_kept_until_its_row_is_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The marker outlives the pruner's window while its row still owes ``cancelled``."""
    from kiro_crew.subagent import write_queued_stop_tombstone

    monkeypatch.setattr("kiro_crew.subagent_persistence._SUBAGENTS_DIR", tmp_path)
    mgr = await _manager(monkeypatch, pump_off_loop=True)
    try:
        store = mgr._taskq
        (rid,) = store.accept([model.TaskRecord(id="q1", kind=model.KIND_SUBAGENT)])
        create_agent_folder(rid, task="t", parent_session=_PARENT)
        write_queued_stop_tombstone(rid)
        path = tmp_path / rid / "tombstone.json"
        aged = json.loads(path.read_text(encoding="utf-8"))
        aged["died"] = time.time() - 8 * 86400
        path.write_text(json.dumps(aged), encoding="utf-8")
        keep = mgr._monitor._refusal_tombstone_unsettled

        assert prune_stale_tombstones(7, 3600, keep) == 0
        assert store.cancel(rid, reason="user_stop") is not None
        assert prune_stale_tombstones(7, 3600, keep) == 1
    finally:
        await mgr.cancel_all()
        _close(mgr)


def _done(events: list) -> list[str]:
    return [i for etype, _k, i, _x in events if etype == "subagent_done"]


def _stop_all_and_join(mgr: SubagentManager, row_id: str) -> tuple[asyncio.Future, asyncio.Future]:
    """Stop all, and a single Stop of *row_id*. Scheduled in this order, the batch files
    the row before the single Stop first runs, so the Stop joins it."""
    return (
        asyncio.ensure_future(mgr.cancel_for_parent(_PARENT)),
        asyncio.ensure_future(mgr.cancel(row_id)),
    )


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_stop_all_whose_cancel_keeps_missing_counts_the_row_once_it_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stop all's batched cancel and the next retry are refused: Stop all counts the
    row, a single Stop that joined the batch answers, and the row is reported, only
    after a later retry has cancelled it."""
    monkeypatch.setattr(bridge_mod, "_SETTLE_RETRY_BACKOFF_CAP_SECS", DEFAULT_INTERVAL_SECS)
    mgr = await _manager(monkeypatch, pump_off_loop=True, max_concurrent=1)
    try:
        row_id = await _queued_row(mgr)
        assert row_id in [p.get("_preassigned_id") for p in mgr._queue]
        store = mgr._taskq
        events = _record(mgr)
        reported_at_landing: list = []
        refused = _contend(
            monkeypatch,
            store,
            row_id,
            times=2,
            events=events,
            reported_at_landing=reported_at_landing,
        )

        with patch.object(SubagentManager, "_run", new=_park):
            stop, joined = _stop_all_and_join(mgr, row_id)
            assert await await_until(stop, what="Stop all answering") == (0, 1)
            answered_over = store.state_of(row_id)
            assert await await_until(joined, what="the joined Stop answering") is True
            await _settle(mgr)

        assert refused == [row_id, row_id]
        assert answered_over == model.CANCELLED, "Stop all counted an uncancelled row"
        assert reported_at_landing[:1] == [[]], "the stop was reported before its cancel landed"
        assert _done(events) == [row_id]
    finally:
        await mgr.cancel_all()
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_stop_all_on_a_wedged_store_counts_the_row_on_its_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every cancel is refused: past the read bound the marker on disk releases the
    row, so Stop all counts it and the joined Stop answers, while the store holds
    the row out of every claim for ``cancelled``."""
    monkeypatch.setattr(bridge_mod, "_SETTLE_RETRY_BACKOFF_CAP_SECS", DEFAULT_INTERVAL_SECS)
    mgr = await _manager(monkeypatch, pump_off_loop=True, max_concurrent=1)
    try:
        row_id = await _queued_row(mgr)
        store = mgr._taskq
        real_cancel = store.cancel
        _contend(monkeypatch, store, row_id, times=10**6)

        with patch.object(SubagentManager, "_run", new=_park):
            stop, joined = _stop_all_and_join(mgr, row_id)
            assert await await_until(stop, what="Stop all answering") == (0, 1)
            assert await await_until(joined, what="the joined Stop answering") is True

        assert store.state_of(row_id) == model.QUEUED
        assert (read_tombstone(row_id) or {}).get("cause") == "queued_stop"
        assert row_id in store.held_until_settled()
        # The store recovers: the settle still retrying behind the stop lands the cancel.
        monkeypatch.setattr(store, "cancel", real_cancel)
        await async_wait_until(lambda: store.state_of(row_id) == model.CANCELLED)
    finally:
        await mgr.cancel_all()
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_stop_all_past_the_read_bound_publishes_nothing_for_the_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every cancel is refused and the row's marker is not confirmed, so nothing
    releases the row: past the read bound Stop all answers without counting it,
    and the joined single Stop answers, and the row is reported, only once a later
    retry cancels it."""
    monkeypatch.setattr(bridge_mod, "_SETTLE_RETRY_BACKOFF_CAP_SECS", DEFAULT_INTERVAL_SECS)
    mgr = await _manager(monkeypatch, pump_off_loop=True, max_concurrent=1)
    try:
        row_id = await _queued_row(mgr)
        store = mgr._taskq
        events = _record(mgr)
        real_cancel = store.cancel
        _contend(monkeypatch, store, row_id, times=10**6)
        real_marker = type(mgr._admission)._tombstone_queued_stop

        def _written_not_confirmed(agent_id: str) -> bool:
            real_marker(agent_id)
            return False

        monkeypatch.setattr(
            type(mgr._admission), "_tombstone_queued_stop", staticmethod(_written_not_confirmed)
        )

        with patch.object(SubagentManager, "_run", new=_park):
            stop, joined = _stop_all_and_join(mgr, row_id)
            assert await await_until(stop, what="Stop all answering") == (0, 0)
            assert not joined.done(), "the joined Stop answered before the row was ended"
            assert store.state_of(row_id) == model.QUEUED
            assert _done(events) == [], "a row past the read bound was reported stopped"

            monkeypatch.setattr(store, "cancel", real_cancel)
            assert await await_until(joined, what="the joined Stop answering") is True
            await async_wait_until(lambda: _done(events) == [row_id])
            await _settle(mgr)

        assert store.state_of(row_id) == model.CANCELLED
    finally:
        await mgr.cancel_all()
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_single_cancel_queued_past_the_read_bound_answers_false_outside_the_refill_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The parent's ``cancel_queued`` over a store that refuses every cancel, the
    marker unconfirmed: its bounded wait runs after the parent's refill hold is let
    go, it answers False, and the row is reported once a later retry cancels it."""
    monkeypatch.setattr(bridge_mod, "_SETTLE_RETRY_BACKOFF_CAP_SECS", DEFAULT_INTERVAL_SECS)
    mgr = await _manager(monkeypatch, pump_off_loop=True, max_concurrent=1)
    try:
        row_id = await _queued_row(mgr)
        store = mgr._taskq
        events = _record(mgr)
        real_cancel = store.cancel
        _contend(monkeypatch, store, row_id, times=10**6)
        bridge = type(mgr._admission)
        real_marker = bridge._tombstone_queued_stop
        real_wait = bridge.settled_within_bound
        holds_at_wait: list[int] = []

        def _written_not_confirmed(agent_id: str) -> bool:
            real_marker(agent_id)
            return False

        async def _wait(self: Any, agent_ids: Any) -> int:
            holds_at_wait.append(mgr.__dict__.get("_stopping_parents", {}).get(_PARENT, 0))
            return await real_wait(self, agent_ids)

        monkeypatch.setattr(bridge, "_tombstone_queued_stop", staticmethod(_written_not_confirmed))
        monkeypatch.setattr(bridge, "settled_within_bound", _wait)

        with patch.object(SubagentManager, "_run", new=_park):
            stop = asyncio.ensure_future(mgr.cancel_queued(row_id, _PARENT))
            assert await await_until(stop, what="cancel_queued answering") is False
            assert holds_at_wait == [0], "the bounded wait ran inside the refill hold"
            assert _done(events) == [], "a row past the read bound was reported stopped"

            monkeypatch.setattr(store, "cancel", real_cancel)
            await async_wait_until(lambda: _done(events) == [row_id])
            await _settle(mgr)

        assert store.state_of(row_id) == model.CANCELLED
    finally:
        await mgr.cancel_all()
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_parent_end_whose_cancel_keeps_missing_reports_the_row_once_it_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The teardown's cancel and the next retry are refused: the row's report goes
    out only after a later retry has cancelled it."""
    monkeypatch.setattr(bridge_mod, "_SETTLE_RETRY_BACKOFF_CAP_SECS", DEFAULT_INTERVAL_SECS)
    mgr = await _manager(monkeypatch, pump_off_loop=True, max_concurrent=1)
    try:
        row_id = await _queued_row(mgr)
        store = mgr._taskq
        events = _record(mgr)
        reported_at_landing: list = []
        refused = _contend(
            monkeypatch,
            store,
            row_id,
            times=2,
            events=events,
            reported_at_landing=reported_at_landing,
        )

        with patch.object(SubagentManager, "_run", new=_park):
            await mgr.cancel_for_teardown([row_id], parent_session_key=_PARENT, verb="reset")
            await _settle(mgr)

        assert refused == [row_id, row_id]
        assert store.state_of(row_id) == model.CANCELLED
        assert reported_at_landing[:1] == [[]], "the stop was reported before its cancel landed"
        assert _done(events) == [row_id]
    finally:
        await mgr.cancel_all()
        _close(mgr)
