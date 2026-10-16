"""After every exit, the parent is told what the store holds.

Whatever ends a run or a conversation, the last published depth equals the
store's count: a parent end stops its store rows without reporting them home,
its sweep never names a claimed or successor row, and a restart survivor is
owed but not counted as waiting. Shared manager, recorder and waits live in
``queue_depth_reconcile_helpers.py``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from queue_depth_reconcile_helpers import (
    _PARENT,
    PUMP_MODES,
    _card,
    _close,
    _defer,
    _depths,
    _free_slot,
    _manager,
    _park,
    _record,
    _resident_with_resume_entry,
    _settle,
    _store_waiting,
    _until,
)

from kiro_crew.subagent import SubagentInfo, SubagentManager
from kiro_crew.subagent_manager.admission import SpawnAdmissionCoordinator
from kiro_crew.subagent_manager.admission.types import WINDOW_ENTRY_RECOVERING
from kiro_crew.taskq import KIND_SUBAGENT, model
from kiro_crew.taskq.reconcile import reconcile_on_boot

pytestmark = pytest.mark.usefixtures("healthy_host_memory")


# ── after every exit, the parent is told what the store holds ────────────────


async def _end_run(mgr: SubagentManager, info: SubagentInfo) -> None:
    """Finish a parked run the way its own ``finally`` would, report included."""
    info.result = "ok"
    await _free_slot(mgr, info, drain=False)
    await mgr._report_terminal(
        info,
        source="test",
        injection_timeout_reason="delivery timed out",
        mark_delivered_on_success=False,
    )


def _survivor_of_a_restart(mgr: SubagentManager, *, due: bool) -> str:
    """A run the previous gateway incarnation was executing for the parent,
    settled the way the boot reconcile settles it: ``recovering``, unleased.

    *due* backdates the reconcile so its backoff has already passed and the
    refill may hydrate the row into the window; otherwise it waits on disk.
    """
    store = mgr._taskq
    rec = model.TaskRecord(
        id="survivor",
        kind=KIND_SUBAGENT,
        session_key=_PARENT,
        params={"task": "survivor", "parent_session_key": _PARENT},
        side_effect_class=model.SIDE_EFFECT_NONE,
    )
    store.accept_one(rec)
    assert store.claim(rec.id, owner="previous-incarnation") is not None
    assert store.transition(rec.id, model.STARTING)
    assert store.transition(rec.id, model.RUNNING)
    report = reconcile_on_boot(store, now=store.now() - 3600.0 if due else None)
    assert (report.examined, report.recovering) == (1, 1), report
    survivor = store.get(rec.id)
    assert survivor is not None and survivor.state == model.RECOVERING
    return rec.id


_EXITS = [
    "complete",
    "cancel",
    "stop_all",
    "parent_end",
    "session_reset",
    "resume_grant",
    "restart_recovery",
    "restart_recovery_windowed",
]


@pytest.mark.asyncio
@pytest.mark.timeout(60)
@PUMP_MODES
@pytest.mark.parametrize("path", _EXITS)
async def test_after_each_exit_the_published_depth_equals_the_store_count(
    monkeypatch: pytest.MonkeyPatch, pump_off_loop: bool, path: str
) -> None:
    """Whatever ends, the last depth the parent is told is what the store holds,
    and what the dashboard's card then shows.

    The parent holds a running run, a resident run parked on its resume entry,
    a row in the window and one the memory gate deferred: every place a
    waiting row, or something that looks like one, can sit. The pump is held
    closed for the exit, as the gateway holds it before its memory barrier, so
    a drain's own emit cannot stand in for the exit's: the frame checked is
    one the exit itself sent. A start the pump makes is checked the same way
    by :func:`test_a_row_the_pump_starts_costs_at_most_two_exact_frames`.

    A parent end stops the deferred row too, though no snapshot can name a row
    held only by the store; a session reset does the same for the conversation
    it ends and spares the row its successor queues before the sweep runs. A
    resume grant answers the card as a settle point. A run that survived a
    restart is never a spawn waiting to start, on disk or hydrated into the
    window, so the first settle point after the boot leaves it out.
    """
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop, max_concurrent=1)
    try:
        resident, running = await _resident_with_resume_entry(mgr)
        with patch.object(SubagentManager, "_run", new=_park):
            waiting = mgr.spawn("waiting", parent_session_key=_PARENT)
            (deferred,) = _defer(mgr, 1)
            await _settle(mgr)
        assert waiting.id in {q.get("_preassigned_id") for q in mgr._queue}
        assert mgr.queued_count_for(_PARENT) == 2
        mgr._queue_dispatch_held = True
        events = _record(mgr)
        successor: SubagentInfo | None = None

        if path == "complete":
            await _end_run(mgr, mgr._agents[running.id])
        elif path == "cancel":
            assert await mgr.cancel(deferred.id) is True
        elif path == "stop_all":
            await mgr.cancel_for_parent(_PARENT)
        elif path in ("parent_end", "session_reset"):
            selected = mgr.snapshot_teardown_children(_PARENT)
            assert deferred.id not in selected
            if path == "session_reset":
                # The successor conversation under the same key queues its
                # first spawn while the reset is still tearing the old one down.
                (successor,) = _defer(mgr, 1)
            await mgr.cancel_for_teardown(selected, parent_session_key=_PARENT, verb=path)
        elif path == "resume_grant":
            # The pump's grant, one entry: the reservation and the durable
            # wake, inline or split across the writer thread as each pump does.
            (index,) = [i for i, q in enumerate(mgr._queue) if q.get("_resume_id") == resident.id]
            entry = mgr._queue.pop(index)
            if pump_off_loop:
                assert mgr._admission.resume_reserve(entry)
                assert await mgr._admission.resume_grant_async(entry)
            else:
                assert mgr._admission.resume_grant(entry)
        else:
            windowed = path == "restart_recovery_windowed"
            survivor = _survivor_of_a_restart(mgr, due=windowed)
            if windowed:
                # The boot pump's first refill hydrates it beside the window row.
                mgr._admission.taskq_refill_window()
                assert survivor in {q.get("_preassigned_id") for q in mgr._queue}
            await _end_run(mgr, mgr._agents[running.id])
        await _settle(mgr)

        published = _depths(events)
        assert published, "the exit never told the parent its depth"
        assert _card(events) == published[-1]["queued"] == _store_waiting(mgr)
        if path in ("stop_all", "parent_end"):
            assert published[-1] == {"queued": 0}
        if path == "session_reset":
            assert successor is not None
            assert published[-1]["queued"] == 1
            row = mgr._taskq.get(successor.id)
            assert row is not None and row.state == model.QUEUED, "the successor's row was swept"
            for retired in (waiting.id, deferred.id):
                gone = mgr._taskq.get(retired)
                assert gone is not None and gone.state == model.CANCELLED
        if path.startswith("restart_recovery"):
            assert published[-1]["queued"] == 2
    finally:
        mgr._queue_dispatch_held = False
        await mgr.cancel_all()
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@PUMP_MODES
async def test_a_parent_end_stops_its_store_rows_without_reporting_them_home(
    monkeypatch: pytest.MonkeyPatch, pump_off_loop: bool
) -> None:
    """A row held only by the store is the ended conversation's work too: it is
    cancelled, and its stop is recorded without injecting into the parent."""
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop)
    try:
        on_done = AsyncMock()
        mgr._on_done = on_done
        deferred = _defer(mgr, 2)
        await _settle(mgr)

        selected = mgr.snapshot_teardown_children(_PARENT)
        assert selected == ()
        stopped = await mgr.cancel_for_teardown(
            selected, parent_session_key=_PARENT, verb="destroy"
        )
        await _settle(mgr)

        assert stopped == 2
        for info in deferred:
            row = mgr._taskq.get(info.id)
            assert row is not None and row.state == model.CANCELLED
        on_done.assert_not_awaited()
        assert _store_waiting(mgr) == 0
        assert mgr._teardown_store_fences == {} and mgr._teardown_store_sweeps == []
    finally:
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@PUMP_MODES
async def test_a_parent_end_sweep_never_names_a_claimed_row(
    monkeypatch: pytest.MonkeyPatch, pump_off_loop: bool
) -> None:
    """A claimed-not-started row is its claimer's, and the teardown refuses to
    cancel one (``allow_admitted=False``). So the sweep's store read leaves it
    out, unlike Stop all's: naming it would put it in the delivery gate (the
    claimer's run would then never report) and in the audit as a stopped row."""
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop)
    try:
        mgr._on_done = AsyncMock()
        claimed, waiting = _defer(mgr, 2)
        await _settle(mgr)
        # Claimed past its deferral, as the pump claims it once that lapses; the
        # clock is put back so the other row stays deferred.
        store = mgr._taskq
        real_clock = store._clock
        monkeypatch.setattr(store, "_clock", lambda: real_clock() + 3600.0)
        assert store.claim(claimed.id, owner="a-claimer-in-flight") is not None
        monkeypatch.setattr(store, "_clock", real_clock)

        selected = mgr.snapshot_teardown_children(_PARENT)
        stopped = await mgr.cancel_for_teardown(
            selected, parent_session_key=_PARENT, verb="destroy"
        )
        await _settle(mgr)

        assert stopped == 1
        gone = mgr._taskq.get(waiting.id)
        assert gone is not None and gone.state == model.CANCELLED
        row = mgr._taskq.get(claimed.id)
        assert row is not None and row.state == model.ADMITTED
        assert claimed.id not in mgr._teardown_cancelled_ids
    finally:
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@PUMP_MODES
async def test_a_wall_clock_stepped_back_mid_teardown_never_sweeps_the_successors_row(
    monkeypatch: pytest.MonkeyPatch, pump_off_loop: bool
) -> None:
    """The host's clock is stepped back an hour between the snapshot and the
    successor's first spawn (an NTP correction): the store stamps the
    successor's row EARLIER than the retired conversation's. The sweep orders
    by accept, not by that stamp, so the retired row is stopped and the
    successor's is left queued."""
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop)
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(mgr._taskq, "_clock", lambda: clock["now"])
    try:
        (retired,) = _defer(mgr, 1)
        await _settle(mgr)
        clock["now"] += 1.0
        selected = mgr.snapshot_teardown_children(_PARENT)
        clock["now"] -= 3600.0
        (successor,) = _defer(mgr, 1)
        successor_row = mgr._taskq.get(successor.id)
        retired_row = mgr._taskq.get(retired.id)
        assert successor_row is not None and retired_row is not None
        assert successor_row.created_at < retired_row.created_at

        await mgr.cancel_for_teardown(selected, parent_session_key=_PARENT, verb="session_reset")
        await _settle(mgr)

        row = mgr._taskq.get(successor.id)
        assert row is not None and row.state == model.QUEUED, "the successor's row was swept"
        gone = mgr._taskq.get(retired.id)
        assert gone is not None and gone.state == model.CANCELLED
    finally:
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@PUMP_MODES
async def test_a_successor_row_accepted_while_the_teardown_cancel_runs_is_spared(
    monkeypatch: pytest.MonkeyPatch, pump_off_loop: bool
) -> None:
    """The successor queues its spawn after the cancel has taken the snapshot's
    fence, while it is still stopping the named runs: the fence keeps recording
    until the sweep has read the store, so that row is spared too, and nothing
    is left recording once the cancel returns."""
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop)
    real = SpawnAdmissionCoordinator.taskq_pending_ids_for_async
    late: list[SubagentInfo] = []

    async def _successor_spawns_first(self: Any, parent: str, **kwargs: Any) -> list[str]:
        late.extend(_defer(mgr, 1))
        return await real(self, parent, **kwargs)

    monkeypatch.setattr(
        SpawnAdmissionCoordinator, "taskq_pending_ids_for_async", _successor_spawns_first
    )
    try:
        (retired,) = _defer(mgr, 1)
        await _settle(mgr)
        selected = mgr.snapshot_teardown_children(_PARENT)

        await mgr.cancel_for_teardown(selected, parent_session_key=_PARENT, verb="session_reset")
        await _settle(mgr)

        (successor,) = late
        row = mgr._taskq.get(successor.id)
        assert row is not None and row.state == model.QUEUED, "the successor's row was swept"
        gone = mgr._taskq.get(retired.id)
        assert gone is not None and gone.state == model.CANCELLED
        assert mgr._teardown_store_fences == {} and mgr._teardown_store_sweeps == []
    finally:
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_teardown_cancel_no_snapshot_preceded_sweeps_no_store_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sweep is fenced by the snapshot; with none taken (a recycle takes no
    snapshot, and its children deliver into the resumed conversation) no store
    row is touched."""
    mgr = await _manager(monkeypatch, pump_off_loop=True)
    try:
        (deferred,) = _defer(mgr, 1)
        await _settle(mgr)

        assert await mgr.cancel_for_teardown((), parent_session_key=_PARENT) == 0
        await _settle(mgr)

        row = mgr._taskq.get(deferred.id)
        assert row is not None and row.state == model.QUEUED
    finally:
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@PUMP_MODES
async def test_a_parent_end_sweeps_a_row_the_refill_windowed_after_its_snapshot(
    monkeypatch: pytest.MonkeyPatch, pump_off_loop: bool
) -> None:
    """A row the retired conversation's store held when the snapshot was taken
    is swept even if the refill pulls it into the window before the sweep reads:
    the sweep keeps the window's rows in its read, and the snapshot's fence is
    what tells it from a successor's row. Left out, the row would stay queued
    and start into whatever the key serves next."""
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop)
    try:
        mgr._queue_dispatch_held = True
        rec = model.TaskRecord(
            id="store-only",
            kind=KIND_SUBAGENT,
            session_key=_PARENT,
            params={"task": "store-only", "parent_session_key": _PARENT},
            side_effect_class=model.SIDE_EFFECT_NONE,
        )
        mgr._taskq.accept_one(rec)
        assert rec.id not in {q.get("_preassigned_id") for q in mgr._queue}

        selected = mgr.snapshot_teardown_children(_PARENT)
        assert rec.id not in selected
        # The teardown's awaits let the pump refill run before the sweep reads.
        mgr._admission.taskq_refill_window()
        assert rec.id in {q.get("_preassigned_id") for q in mgr._queue}
        await mgr.cancel_for_teardown(selected, parent_session_key=_PARENT, verb="destroy")
        await _settle(mgr)

        row = mgr._taskq.get(rec.id)
        assert row is not None and row.state == model.CANCELLED
        assert rec.id not in {q.get("_preassigned_id") for q in mgr._queue}
        assert _store_waiting(mgr) == 0
    finally:
        mgr._queue_dispatch_held = False
        await mgr.cancel_all()
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@PUMP_MODES
async def test_a_restart_survivor_the_gate_requeues_stays_off_the_card(
    monkeypatch: pytest.MonkeyPatch, pump_off_loop: bool
) -> None:
    """The pump pops a restart survivor from the window and the gate puts it
    back (here the child reserve; a stagger tick or a cap a sibling's start
    filled does the same). The row is still unclaimed, so still ``recovering``:
    the entry the gate appends keeps the window's mark, and the card never shows
    the survivor as waiting to start, while the parent is still owed it."""
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop)
    refusals: list[int] = []

    def _reserved(_self: Any) -> bool:
        # One refusal, then the pump is held: a reserve that stayed closed
        # would have the pump re-try the entry on every recheck.
        refusals.append(1)
        mgr._queue_dispatch_held = True
        return False

    monkeypatch.setattr(SpawnAdmissionCoordinator, "root_may_start", _reserved)
    try:
        mgr._queue_dispatch_held = True
        survivor = _survivor_of_a_restart(mgr, due=True)
        mgr._admission.taskq_refill_window()
        assert survivor in {q.get("_preassigned_id") for q in mgr._queue}
        events = _record(mgr)

        with patch.object(SubagentManager, "_run", new=_park):
            mgr._queue_dispatch_held = False
            mgr._drain_queue()
            await _settle(mgr)

        assert refusals == [1], "the gate never re-checked the child reserve"
        row = mgr._taskq.get(survivor)
        assert row is not None and row.state == model.RECOVERING
        published = _depths(events)
        assert published, "the re-queue never told the parent its depth"
        assert [frame["queued"] for frame in published] == [0] * len(published)
        assert _card(events) == 0 == _store_waiting(mgr)
        (entry,) = [q for q in mgr._queue if q.get("_preassigned_id") == survivor]
        assert entry.get(WINDOW_ENTRY_RECOVERING) is True
        assert mgr.queued_count_for(_PARENT) == 1
    finally:
        mgr._queue_dispatch_held = False
        await mgr.cancel_all()
        _close(mgr)


@pytest.mark.parametrize("state", [model.QUEUED, model.RECOVERING])
def test_the_recovering_mark_comes_from_the_row_state_not_its_params(state: str) -> None:
    """The window's ``recovering`` mark is read off the row's state alone: a
    stale copy carried in a row's params never hides a spawn waiting to start
    from the chip, and a ``recovering`` row is marked whatever its params say."""
    rec = model.TaskRecord(
        id="row",
        kind=KIND_SUBAGENT,
        session_key=_PARENT,
        params={"task": "row", "parent_session_key": _PARENT, WINDOW_ENTRY_RECOVERING: True},
        state=state,
    )
    entry = SpawnAdmissionCoordinator._window_entry(rec)
    assert entry.get(WINDOW_ENTRY_RECOVERING, False) is (state == model.RECOVERING)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@PUMP_MODES
async def test_a_restart_survivor_in_the_window_is_owed_but_not_waiting_and_starts(
    monkeypatch: pytest.MonkeyPatch, pump_off_loop: bool
) -> None:
    """The guards that hold a parent's reset still count a run that survived a
    restart; the card does not; and its window entry still starts, the mark
    handed to ``spawn`` as its keyword."""
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop)
    try:
        mgr._queue_dispatch_held = True
        survivor = _survivor_of_a_restart(mgr, due=True)
        mgr._admission.taskq_refill_window()
        assert survivor in {q.get("_preassigned_id") for q in mgr._queue}
        events = _record(mgr)

        mgr._emit_queue_depth(_PARENT)
        await _settle(mgr)

        assert _depths(events) == [{"queued": 0}]
        assert mgr.queued_count_for(_PARENT) == 1
        assert await mgr.queued_count_for_async(_PARENT) == 1

        with patch.object(SubagentManager, "_run", new=_park):
            mgr._queue_dispatch_held = False
            mgr._drain_queue()
            await _settle(mgr)
            await _until(lambda: survivor in mgr._tasks, "the survivor started")
        assert not mgr._queue
        assert all(frame["queued"] == 0 for frame in _depths(events))
    finally:
        mgr._queue_dispatch_held = False
        await mgr.cancel_all()
        _close(mgr)
