"""A single Stop of a queued run is published only once the row is durably ended.

The Stop drops the run's window entry and registers its stopped record at once,
which is what the claimer refuses. When the row's store cancel does not land (a
contended store raising ``TaskStoreUnavailable``) the stop is not told to the
caller on that alone: the row's queued-stop marker goes to disk first, the
cancel is retried until it lands, and the report and ``cancel``'s answer wait for
it. Past the read bound, the marker confirmed on disk is the verdict instead:
the store holds the row out of every claim and the next boot settles it
``cancelled``.
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
from kiro_crew.testing.wait import DEFAULT_INTERVAL_SECS, async_wait_until

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
