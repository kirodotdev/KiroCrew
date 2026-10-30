"""The work ledger rebuild's guards, the dirty flag and the board generation.

A pruned unit refuses the rebuild instead of erasing its reports, a dirty cache refuses
every route until a rebuild clears it, a cancelled record drains its append, a rebuild
holds a fold-created item's lock through its undo, and every write carries the board's
generation: a write whose header cannot be read is refused, a generationless straggler
is omitted once a stamped board is live, and a new generation starts its own count.
"""

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace
from typing import Any

import pytest
import work_ledger_projection_helpers
from off_loop_helpers import off_loop
from work_ledger_projection_helpers import (
    _SLOTS,
    ACCEPTANCE,
    CONDUCTOR,
    WORKER,
    _board,
    _real_units,
    _rebuild,
    _record,
    _rendered,
    _report,
    _req,
    _Slot,
)

from kiro_crew import work_ledger as wl
from kiro_crew.crew_log import eager, projection
from kiro_crew.dashboard.handlers import work_ledger as routes
from kiro_crew.testing.wait import await_until, until_parked

# The shared fixtures, bound here by name so pytest registers them for this module.
_isolated = work_ledger_projection_helpers._isolated
recorded = work_ledger_projection_helpers.recorded


# -- lineage on a baseline; a pruned unit refuses; the dirty flag ----------------


async def _get(handler: Any, path: str, sk: str) -> tuple[int, dict[str, Any]]:
    resp = await handler(_req("GET", path, sk=sk))
    return resp.status, json.loads(resp.text)


@pytest.mark.asyncio
async def test_a_nested_legacy_boards_baseline_carries_its_lineage(monkeypatch):
    """A nested board from before the projection: its only recorded write is a
    worker's report. The baseline carries depth, parent_item and goal, and the
    board rebuilt from it is nested, not top-level."""
    wl.ensure_conductor(CONDUCTOR, goal="sub-goal", depth=1, parent_item="it_0000aaaa")
    old = wl.apply_conductor_action(CONDUCTOR, "create", title="old", acceptance=ACCEPTANCE)["item"]
    _SLOTS[WORKER] = _Slot(created_by=CONDUCTOR)
    wl.apply_conductor_action(CONDUCTOR, "bind", item_id=old.item_id, worker_session_key=WORKER)
    _real_units(monkeypatch)

    status, body = await _report(WORKER, {"status": "progress", "summary": "first recorded"})
    assert status == 200, body

    def _worker_entries() -> list[dict[str, Any]]:
        handle = projection.open_session_log("u-worker")
        return [
            e.data
            for e in handle.iter_from(1, known=projection.KNOWN_TYPES)
            if e.type == "work/recorded"
        ]

    # Off the loop: the eager folder reads the unit on its own thread after the append.
    entries = off_loop(_worker_entries)
    assert (entries[-1]["baseline"], entries[-1]["depth"], entries[-1]["parent_item"]) == (
        True,
        1,
        "it_0000aaaa",
    )
    assert entries[-1]["goal"] == "sub-goal"

    (wl.conductor_dir(CONDUCTOR) / "conductor.json").unlink()
    wl.item_path(CONDUCTOR, old.item_id).unlink()
    wl.rebuild_from_projection(CONDUCTOR)
    header = wl.read_conductor(CONDUCTOR)
    assert (header.depth, header.parent_item, header.goal) == (1, "it_0000aaaa", "sub-goal")
    assert wl.read_work_item(CONDUCTOR, old.item_id).summary == "first recorded"


@pytest.mark.asyncio
async def test_a_pruned_worker_unit_leaves_the_rebuilt_report_standing(
    monkeypatch,
):
    """The board's copy of the report is in the conductor's log, so retention taking the
    worker's unit costs the board nothing: the rebuild keeps the report."""
    from kiro_crew.crew_log import store

    _real_units(monkeypatch)
    item_id = await _board()
    status, body = await _report(WORKER, {"status": "progress", "summary": "half way"})
    assert status == 200, body
    before = wl.read_work_item(CONDUCTOR, item_id)

    # Retention takes the worker's unit; the conductor's create and bind remain.
    import shutil

    from kiro_crew import crew_log as lg

    with eager.paused():
        shutil.rmtree(store.crew_log_dir(lg.KIND_SESSION, "u-worker"))
    wl.rebuild_from_projection(CONDUCTOR)
    after = wl.read_work_item(CONDUCTOR, item_id)
    assert (after.status, after.summary, after.last_report_at) == (
        before.status,
        before.summary,
        before.last_report_at,
    )


@pytest.mark.asyncio
async def test_a_rebuild_keeps_the_completeness_marker_and_a_later_prune_keeps_the_report(
    monkeypatch,
):
    """Entries never carry ``recorded_at``; a rebuild that copied the fold verbatim
    would clear it, so the stamp must survive the fold. A retention prune of the
    worker's unit and a second rebuild then keep the report, whose board copy is in
    the conductor's log."""
    import shutil

    from kiro_crew import crew_log as lg
    from kiro_crew.crew_log import store

    _real_units(monkeypatch)
    item_id = await _board()
    status, body = await _report(WORKER, {"status": "progress", "summary": "half way"})
    assert status == 200, body
    stamped = wl.read_work_item(CONDUCTOR, item_id)
    assert stamped is not None and stamped.recorded_at

    counts = wl.rebuild_from_projection(CONDUCTOR)
    assert counts["items"] == 1
    rebuilt = wl.read_work_item(CONDUCTOR, item_id)
    assert rebuilt is not None
    assert rebuilt.recorded_at == stamped.recorded_at
    assert (rebuilt.status, rebuilt.summary) == ("progress", "half way")

    with eager.paused():
        shutil.rmtree(store.crew_log_dir(lg.KIND_SESSION, "u-worker"))
    wl.rebuild_from_projection(CONDUCTOR)
    after = wl.read_work_item(CONDUCTOR, item_id)
    assert after is not None
    assert (after.status, after.summary, after.last_report_at) == (
        "progress",
        "half way",
        stamped.last_report_at,
    )


def test_an_item_the_cache_never_held_is_stamped_recorded_by_the_rebuild(monkeypatch):
    """A rebuild into an empty cache has no stamp to keep: the fold holding the
    item whole is what the stamp asserts, so the rebuild sets it."""
    item_id = "it_0000abcd"
    monkeypatch.setattr(
        projection,
        "read_slot_projection",
        lambda slot, name, **_kw: SimpleNamespace(
            value=_rendered(slot, item_id) if name == "work" else {}
        ),
    )
    wl.rebuild_from_projection(CONDUCTOR)
    item = wl.read_work_item(CONDUCTOR, item_id)
    assert item is not None and item.recorded_at


@pytest.mark.asyncio
async def test_a_dirty_cache_refuses_every_route_until_a_rebuild_clears_it(monkeypatch):
    _real_units(monkeypatch)
    item_id = await _board()
    wl.mark_cache_dirty(CONDUCTOR, "an unrecorded write could not be undone")

    status, body = await _report(WORKER, {"status": "progress", "summary": "x"})
    assert (status, body["code"]) == (409, "cache_dirty")
    # The refusal names both exits: a rebuild, or removing the marker to keep the cache.
    assert "work_ledger_rebuild" in body["error"]
    assert f"{wl.DIRTY_FILE!r} marker" in body["error"]
    status, body = await _get(routes.api_work_brief, "/api/work-ledger/brief", WORKER)
    assert (status, body["code"]) == (409, "cache_dirty")
    status, body = await _get(routes.api_work_ledger_get, "/api/work-ledger", CONDUCTOR)
    assert (status, body["code"]) == (409, "cache_dirty")
    status, body = await _record(
        CONDUCTOR, {"action": "decide", "item_id": item_id, "decision": "go"}
    )
    assert (status, body["code"]) == (409, "cache_dirty")
    assert wl.read_work_item(CONDUCTOR, item_id).decision == ""

    status, body = await _rebuild(CONDUCTOR)
    assert status == 200, body
    assert wl.cache_dirty(CONDUCTOR) is None
    status, body = await _get(routes.api_work_ledger_get, "/api/work-ledger", CONDUCTOR)
    assert status == 200, body


@pytest.mark.asyncio
async def test_a_write_queued_behind_the_lock_rechecks_the_dirty_flag_under_it(monkeypatch):
    """The early dirty check runs before the board lock. A write ahead of this one
    can mark the cache dirty (its undo failed) after that check passed; the write
    must look again once it holds the lock, or it commits on the damage."""
    import asyncio

    _real_units(monkeypatch)
    item_id = await _board()

    async def _hold_the_board() -> tuple[asyncio.Future[None], asyncio.Event]:
        """Hold CONDUCTOR's board lock in a helper task until the returned event is set."""
        held, release = asyncio.Event(), asyncio.Event()

        async def _holder() -> None:
            async with routes._board_lock(CONDUCTOR):
                held.set()
                await await_until(release)

        holder = asyncio.ensure_future(_holder())
        await await_until(held)
        return holder, release

    holder, release = await _hold_the_board()
    queued = asyncio.ensure_future(
        _record(CONDUCTOR, {"action": "decide", "item_id": item_id, "decision": "go"})
    )
    await until_parked(
        routes._BOARD_LOCKS._locks[CONDUCTOR][0],
        describe=lambda: f"queued.done()={queued.done()}",
    )
    assert not queued.done()  # past the early check, waiting on the lock
    wl.mark_cache_dirty(CONDUCTOR, "the write ahead could not be undone")
    release.set()
    await await_until(holder)

    # flake-ok: pre-existing unbounded await of the queued write once the lock holder is released; moved verbatim, tracked for a de-flake pass
    status, body = await queued
    assert (status, body["code"]) == (409, "cache_dirty"), body
    assert wl.read_work_item(CONDUCTOR, item_id).decision == ""

    # The same race on the worker's route.
    wl.clear_cache_dirty(CONDUCTOR)
    holder, release = await _hold_the_board()
    queued = asyncio.ensure_future(_report(WORKER, {"status": "progress", "summary": "x"}))
    await until_parked(
        routes._BOARD_LOCKS._locks[CONDUCTOR][0],
        describe=lambda: f"queued.done()={queued.done()}",
    )
    assert not queued.done()
    wl.mark_cache_dirty(CONDUCTOR, "the write ahead could not be undone")
    release.set()
    await await_until(holder)
    # flake-ok: pre-existing unbounded await of the queued write once the lock holder is released; moved verbatim, tracked for a de-flake pass
    status, body = await queued
    assert (status, body["code"]) == (409, "cache_dirty"), body
    assert wl.read_work_item(CONDUCTOR, item_id).status is None


@pytest.mark.asyncio
async def test_a_failed_undo_flags_the_cache_dirty(monkeypatch):
    _real_units(monkeypatch, worker_lands=False)
    item_id = await _board()

    def _broken(*_a: Any, **_k: Any) -> None:
        raise OSError("disk gone")

    monkeypatch.setattr(routes.work_ledger, "restore_snapshot", _broken)
    status, body = await _report(WORKER, {"status": "progress", "summary": "lost"})
    assert (status, body["code"]) == (503, "crew_log_unrecorded")
    assert wl.cache_dirty(CONDUCTOR) == "an unrecorded write could not be undone"
    status, body = await _get(routes.api_work_ledger_get, "/api/work-ledger", CONDUCTOR)
    assert (status, body["code"]) == (409, "cache_dirty")
    assert item_id


@pytest.mark.asyncio
async def test_a_reused_slot_is_not_overwritten_when_its_first_goal_never_appended(monkeypatch):
    """A slot's SECOND board must not be replaced by the first one the log still holds.

    The neighbouring guard compares ``goal_version`` only when the cached header
    carries ``recorded_at``, and ``recorded_at`` is stamped only once a goal entry
    has landed. So a board's FIRST goal -- committed to the cache, its append lost
    to the crash window -- leaves the header unstamped and skips the guard, which is
    the very case the guard's own comment says it exists for.

    That matters on a REUSED slot. The crew log is append-only, so the first board's
    entries outlive its ledger directory: the fold can still produce the purged board
    forever after. Adopting it would silently replace the live board with the dead
    one, and nothing afterwards takes that back -- the generation is the only thing
    that distinguishes them, and it is minted once per board.
    """
    from datetime import timedelta

    _real_units(monkeypatch)
    item_id = await _board()
    board_a = wl.read_conductor(CONDUCTOR)
    assert board_a.recorded_at and board_a.generation

    # Board A finishes and the sweep reaps its directory. Its ENTRIES remain.
    status, body = await _record(
        CONDUCTOR, {"action": "close", "item_id": item_id, "state": "accepted"}
    )
    assert status == 200, body
    assert wl.purge_conductor(CONDUCTOR, allow_unreadable=False, idle_for=timedelta(0))
    assert wl.read_conductor(CONDUCTOR) is None

    # The slot is reused. Board B's first goal commits; its entry never lands.
    #
    # This models the CRASH window, not a refused append: the process dies between
    # the cache commit and the record append, so nothing observes a failure. Hence
    # both stubs. ``on_work_recorded`` answering True is what keeps the handler's
    # rollback from firing, and suppressing the stamp is what leaves ``recorded_at``
    # empty -- a stub that only did the first would still stamp the header, because
    # the handler would believe the entry had landed.
    landed = routes.crew_log_emit.on_work_recorded
    stamp = routes._mark_goal_recorded
    monkeypatch.setattr(routes.crew_log_emit, "on_work_recorded", lambda unit, data: True)
    monkeypatch.setattr(routes, "_mark_goal_recorded", lambda slot: None)
    status, body = await _record(CONDUCTOR, {"action": "goal", "goal": "second life", "round": 1})
    assert status == 200, body
    monkeypatch.setattr(routes.crew_log_emit, "on_work_recorded", landed)
    monkeypatch.setattr(routes, "_mark_goal_recorded", stamp)

    board_b = wl.read_conductor(CONDUCTOR)
    assert board_b.recorded_at == ""  # the guard's precondition is falsy here
    assert board_b.generation and board_b.generation != board_a.generation

    # The fold can only produce A, so the rebuild must refuse rather than adopt it.
    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.rebuild_from_projection(CONDUCTOR)
    assert caught.value.code == wl.CODE_CREW_LOG_INCOMPLETE

    # B survives. Asserted on the GENERATION, not just the goal text: the goal could
    # match by coincidence, whereas the generation is what identifies the board.
    kept = wl.read_conductor(CONDUCTOR)
    assert kept.generation == board_b.generation
    assert kept.goal == "second life"


@pytest.mark.asyncio
async def test_a_first_goal_the_record_holds_still_rebuilds(monkeypatch):
    """The partner to the refusal above, and the reason it means anything.

    A guard that refused EVERY board whose header is unstamped would satisfy the
    test above just as well as a correct one. This pins the other side: a board
    whose goal did reach the record rebuilds, and keeps its own generation.
    """
    _real_units(monkeypatch)
    await _board()
    before = wl.read_conductor(CONDUCTOR)
    assert before.recorded_at and before.generation

    wl.rebuild_from_projection(CONDUCTOR)

    after = wl.read_conductor(CONDUCTOR)
    assert after.generation == before.generation
    assert after.goal == "ship it"


def test_supplemental_worker_units_use_append_order_not_the_header_clock(monkeypatch):
    """An ``also_slots`` worker's later report wins even when its header clock is older.

    The sibling of ``test_work_fold_uses_append_order_when_header_clocks_go_backward``,
    for the OTHER route units reach this fold by. A worker named through ``also_slots``
    -- one bound before the board was recorded -- does not come from the fold's own unit
    list, so it was still being ordered by the header clock while every other unit in the
    same fold was ordered causally. Both land in one fold and a later entry applies over
    an earlier one, so that inconsistency let a backward clock step across the worker's
    reset fold its OLDER report last, overwriting the latest status and summary.
    """
    from kiro_crew import crew_log as lg
    from kiro_crew.crew_log import CrewLog, emit, store

    older_unit = "u-worker-clock-older"
    newer_unit = "u-worker-clock-newer"
    monkeypatch.setattr(emit, "enabled", lambda: True)

    wl.ensure_conductor(CONDUCTOR, goal="ship it")
    created = wl.apply_conductor_action(
        CONDUCTOR, "create", title="item one", acceptance=ACCEPTANCE
    )
    item_id = created["item"].item_id
    _SLOTS[WORKER] = _Slot(created_by=CONDUCTOR)

    # The board's own entries, in the conductor's unit: the fold builds an item row from
    # the create, and the worker's reports below land on that row.
    board_unit = "u-board-clock"
    monkeypatch.setattr(store, "now_ms", lambda: 3_000)
    opened = CrewLog.create(
        lg.KIND_SESSION, board_unit, owner="raymond", agent="kirocrew", slot=CONDUCTOR
    )
    del opened
    mine = {"slot": CONDUCTOR, "actor": "conductor", "by": CONDUCTOR}
    assert emit.on_work_recorded(
        board_unit, {**mine, "action": "goal", "goal": "ship it", "round": 1, "depth": 0}
    )
    assert emit.on_work_recorded(
        board_unit,
        {
            **mine,
            "action": "create",
            "item_id": item_id,
            "title": "item one",
            "acceptance": ACCEPTANCE,
        },
    )

    # The worker's FIRST unit, appended first, carries the later wall clock.
    monkeypatch.setattr(store, "now_ms", lambda: 2_000)
    first = CrewLog.create(
        lg.KIND_SESSION, older_unit, owner="raymond", agent="kirocrew", slot=WORKER
    )
    del first
    assert emit.on_work_recorded(
        older_unit,
        {
            "slot": CONDUCTOR,
            "actor": "worker",
            "by": WORKER,
            "action": "report",
            "item_id": item_id,
            "status": "blocked",
            "summary": "actually older",
        },
    )

    # The worker resets. Its replacement unit is created and appended LATER, but an NTP
    # correction gives it the earlier header clock, so the raw store order is backwards.
    monkeypatch.setattr(store, "now_ms", lambda: 1_000)
    second = CrewLog.create(
        lg.KIND_SESSION, newer_unit, owner="raymond", agent="kirocrew", slot=WORKER
    )
    del second
    assert emit.on_work_recorded(
        newer_unit,
        {
            "slot": CONDUCTOR,
            "actor": "worker",
            "by": WORKER,
            "action": "report",
            "item_id": item_id,
            "status": "progress",
            "summary": "actually newer",
        },
    )

    folded = projection.read_slot_projection(CONDUCTOR, "work", also_slots=[WORKER])
    items = {row["item_id"]: row for row in folded.value["items"]}
    assert items[item_id]["summary"] == "actually newer"
    assert items[item_id]["status"] == "progress"


@pytest.mark.asyncio
async def test_a_dirty_cache_whose_write_outran_the_record_can_still_rebuild(monkeypatch):
    """The dirty flag's documented cure has to work in the state that SETS the flag.

    ``test_a_dirty_cache_refuses_every_route_until_a_rebuild_clears_it`` already shows a
    rebuild clearing the flag, but on a board whose cache is NOT ahead of the record --
    every write in that fixture was recorded. The flag is set by a different situation:
    an unrecorded write whose undo ALSO failed, which is what leaves the cache holding a
    mutation the fold does not have.

    That is precisely the state :func:`_refuse_fold_behind_cache` exists to refuse, so it
    is the state in which the cure could be blocked by the guard -- the refusal names a
    rebuild as the way out while the rebuild refuses because the cache is ahead. If that
    were so, the board would be locked with its only documented exit closed, which no
    amount of operator patience resolves. This pins the cure to the situation rather than
    to a convenient fixture.
    """
    _real_units(monkeypatch, worker_lands=False)
    item_id = await _board()
    healthy_undo = routes.work_ledger.restore_snapshot

    def _broken(*_a: Any, **_k: Any) -> None:
        raise OSError("disk gone")

    monkeypatch.setattr(routes.work_ledger, "restore_snapshot", _broken)
    status, body = await _report(WORKER, {"status": "progress", "summary": "lost"})
    assert (status, body["code"]) == (503, "crew_log_unrecorded")
    assert wl.cache_dirty(CONDUCTOR) == "an unrecorded write could not be undone"

    # The operator's recovery runs in a healthy process: the undo that failed is not
    # broken for all time, and the rebuild does not depend on it anyway.
    monkeypatch.setattr(routes.work_ledger, "restore_snapshot", healthy_undo)

    status, body = await _rebuild(CONDUCTOR)
    assert status == 200, body
    assert wl.cache_dirty(CONDUCTOR) is None
    assert item_id


def test_a_worker_baseline_does_not_regress_a_recorded_conductor_goal(monkeypatch):
    """A baseline folded after a conductor goal must not put the older goal back.

    ``goal_version``, the goal TEXT and the round travel together, so one comparison has to
    gate all three: a version that admits only a newer write while the text beside it takes
    any later writer leaves the state naming a version it does not hold. A worker baseline
    is the entry that reaches that seam: it carries the board's header fields for a board
    the record never saw set, it is not version-stamped, and it can fold after the
    conductor's own goal.

    The partner case is in
    ``test_a_baseline_still_supplies_the_header_when_the_record_has_no_goal`` -- without it
    a fold that ignored every baseline header field would satisfy this test too.
    """
    state = projection._work_start()
    projection._work_apply_header(
        state, {"action": "goal", "goal": "ship it, then rest", "round": 2, "goal_version": 2}
    )
    assert (state["goal"], state["round"], state["goal_version"]) == ("ship it, then rest", 2, 2)

    # The stale baseline arrives afterwards, carrying the board's ORIGINAL goal and round.
    projection._work_apply_header(state, {"action": "report", "goal": "ship it", "board_round": 1})
    assert state["goal"] == "ship it, then rest"
    assert state["round"] == 2
    assert state["goal_version"] == 2


def test_a_baseline_still_supplies_the_header_when_the_record_has_no_goal(monkeypatch):
    """The partner: a baseline is the ONLY header source for a pre-projection board.

    This is the job the baseline exists for, so the guard above must not cost it. A board
    whose goal write was never recorded has ``goal_version`` 0, and the baseline's fields
    are then the best the record has.
    """
    state = projection._work_start()
    projection._work_apply_header(state, {"action": "report", "goal": "ship it", "board_round": 1})
    assert state["goal"] == "ship it"
    assert state["round"] == 1


@pytest.mark.asyncio
async def test_cancelled_record_drains_its_append_before_reraising(monkeypatch):
    """The record route's transaction drains, like the report route's beside it.

    Both commit the cache and then append the log. A cancellation landing between
    the two leaves a committed cache write the record never saw, and gives the
    board lock back with that divergence in it.
    """
    _real_units(monkeypatch)
    item_id = await _board()
    entered = threading.Event()
    release = threading.Event()
    append = routes.crew_log_emit.on_work_recorded

    def _blocked_append(unit: str, data: dict[str, Any]) -> bool:
        if data.get("action") == "decide":
            entered.set()
            if not release.wait(2):
                raise AssertionError("the cancellation test did not release the append")
        return append(unit, data)

    monkeypatch.setattr(routes.crew_log_emit, "on_work_recorded", _blocked_append)
    pending = asyncio.create_task(
        _record(CONDUCTOR, {"action": "decide", "item_id": item_id, "decision": "ship it"})
    )
    try:
        assert await asyncio.to_thread(entered.wait, 2), "the append worker was not entered"
        pending.cancel()
        # flake-ok: pre-existing 50 ms window showing the cancelled write still drains its append; moved verbatim, tracked for a de-flake pass
        done, _waiting = await asyncio.wait({pending}, timeout=0.05)
        assert not done, "cancellation escaped before the append-or-rollback transaction drained"
    finally:
        release.set()

    with pytest.raises(asyncio.CancelledError):
        # flake-ok: pre-existing unbounded await; the blocked append's own 2 s release bound ends it; moved verbatim, tracked for a de-flake pass
        await pending

    item = wl.read_work_item(CONDUCTOR, item_id)
    assert item is not None and item.decision == "ship it"


@pytest.mark.asyncio
async def test_a_deeply_nested_acceptance_is_refused_before_writing(recorded):
    """Redaction walks every nested string, so the caller chooses its depth.

    Unbounded, that walk exhausts the stack and the ``RecursionError`` leaves the
    route as a 500. The refusal belongs with every other unusable field instead: a
    400, before a lock is taken or a byte written. The bound also means a far
    deeper body is refused at the bound rather than by recursing into it.

    The partner case is
    ``test_non_colliding_nested_acceptance_round_trips_through_cache_and_log``,
    whose three-level acceptance must still reach both the cache and the log --
    without it a bound of zero would satisfy this test.
    """
    wl.ensure_conductor(CONDUCTOR, goal="ship it")
    deep: Any = "bottom"
    for _ in range(64):
        deep = {"nested": deep}

    status, body = await _record(
        CONDUCTOR, {"action": "create", "title": "deep", "acceptance": deep}
    )

    assert (status, body["code"]) == (400, wl.CODE_INVALID_VALUE), body
    assert "deeper than" in body["error"]
    assert recorded == []


def test_a_rebuild_holds_a_fold_created_items_lock_through_its_undo(monkeypatch):
    """The lock of an item the fold CREATES is held until the rebuild is over.

    A plain contention test would pass either way, because the rebuild takes that
    lock in both shapes. What matters is WHEN it lets go. Released as soon as that
    item's files were written, the lock leaves a window: the write routes take an
    item's lock alone and never the conductor lock, so a second gateway commits a
    report there -- and a not-yet-present item's footprint snapshot is ``None``, so
    the undo unlinks that committed write without a trace.

    So this fails the rebuild AFTER the new item lands, and asks at undo time
    whether another holder could take that item's lock. It must not be able to.
    """
    from kiro_crew import crew_log as lg
    from kiro_crew.crew_log import CrewLog

    new_id = "it_0000abcd"
    stray_id = "it_0000dead"
    conductor = CrewLog.create(
        lg.KIND_SESSION, "u-conductor", owner="raymond", agent="kirocrew", slot=CONDUCTOR
    )
    mine = {"slot": CONDUCTOR, "actor": "conductor", "by": CONDUCTOR}
    conductor.append(
        "work/recorded",
        {**mine, "action": "goal", "goal": "recorded goal", "round": 1},
        src="gateway",
    )
    conductor.append(
        "work/recorded",
        {
            **mine,
            "action": "create",
            "item_id": new_id,
            "title": "recorded",
            "event": "recorded",
            "event_kind": "create",
        },
        src="gateway",
    )
    # The fold names `new_id`, which the cache does NOT hold -- so the rebuild
    # creates it. The stray gives the post-loop pass something to read, which is
    # where the failure below is injected.
    wl.ensure_conductor(CONDUCTOR, goal="cache goal")
    stray = wl.item_path(CONDUCTOR, stray_id)
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_text(json.dumps({"item_id": stray_id, "title": "stray"}), encoding="utf-8")

    real_read = wl.read_work_item

    def _fail_once_the_new_item_has_landed(slot: str, item_id: str, *, strict: bool = False):
        if item_id == stray_id and wl.item_path(CONDUCTOR, new_id).exists():
            raise OSError("disk full, after the new item landed")
        return real_read(slot, item_id, strict=strict)

    took_the_lock = threading.Event()
    observed: dict[str, bool] = {}
    real_restore = wl._restore_files

    def _ask_at_undo_time(snapshot):
        probe = threading.Thread(target=_hold_briefly, daemon=True)
        probe.start()
        # flake-ok: pre-existing 1.5 s probe wait whose expiry is the passing answer, on a daemon probe thread it never joins; moved verbatim, tracked for a de-flake pass
        observed["free_at_undo"] = took_the_lock.wait(1.5)
        return real_restore(snapshot)

    def _hold_briefly() -> None:
        with wl.item_lock(CONDUCTOR, new_id, create=False):
            took_the_lock.set()

    monkeypatch.setattr(wl, "read_work_item", _fail_once_the_new_item_has_landed)
    monkeypatch.setattr(wl, "_restore_files", _ask_at_undo_time)

    with pytest.raises(OSError):
        wl.rebuild_from_projection(CONDUCTOR)

    assert observed["free_at_undo"] is False, (
        "the fold-created item's lock was free while the undo ran, so a concurrent "
        "write could land in that window and be unlinked by it"
    )


@pytest.mark.asyncio
async def test_a_pruned_middle_unit_is_detected_when_the_cache_holds_its_cap_of_events(
    monkeypatch,
):
    """The pruned unit does not have to be the newest one.

    At the cap a count comparison says nothing, and checking only the NEWEST cached
    event leaves a middle one unguarded: its unit is gone, the count still matches
    and so does the latest id, so a rebuild would put an older event in its place
    and clear the dirty flag on the way out. Every comparable cached event is
    therefore checked by identity, and this pins that the MIDDLE one is what the
    refusal names -- the newest-only check passes this scenario.
    """
    monkeypatch.setattr(wl, "MAX_EVENTS_PER_ITEM", 3)
    _real_units(monkeypatch)
    item_id = await _board()
    real_append = routes.crew_log_emit.on_work_recorded

    for n in range(2):
        status, body = await _record(
            CONDUCTOR, {"action": "decide", "item_id": item_id, "decision": f"round {n}"}
        )
        assert status == 200, body

    # This decision lands in the cache and its entry never reaches the record. One
    # more recorded write follows, so it ends up in the MIDDLE of the cached window
    # rather than at its end.
    monkeypatch.setattr(routes.crew_log_emit, "on_work_recorded", lambda unit, data: True)
    status, body = await _record(
        CONDUCTOR, {"action": "decide", "item_id": item_id, "decision": "middle, lost"}
    )
    assert status == 200, body

    monkeypatch.setattr(routes.crew_log_emit, "on_work_recorded", real_append)
    status, body = await _record(
        CONDUCTOR, {"action": "decide", "item_id": item_id, "decision": "newest, recorded"}
    )
    assert status == 200, body

    cached = wl.read_events(CONDUCTOR, item_id)
    assert len(cached) == 3, [event.text for event in cached]

    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.rebuild_from_projection(CONDUCTOR)

    assert caught.value.code == wl.CODE_CREW_LOG_INCOMPLETE
    assert cached[1].id in str(caught.value), caught.value
    assert wl.read_work_item(CONDUCTOR, item_id).decision == "newest, recorded"


def test_a_new_generation_resets_the_boards_own_entry_count_and_epoch():
    """``entries`` and ``first_entry_at`` belong to the board, so they reset with it.

    Both are rendered as that board's metadata, and ``first_entry_at`` is the epoch
    that separates an item created before the board's first recorded entry from one
    created after. Carried across a reset, a reused slot's new board inherits the old
    board's entry count and the old board's epoch, which dates its own items as
    predating it.

    Asserted directly as well as through the fold: the epoch's two values can land in
    the same second, so comparing timestamps end to end would be flaky, while the
    reset itself is exact.
    """
    from kiro_crew import crew_log as lg
    from kiro_crew.crew_log import CrewLog

    state = projection._work_start()
    state["entries"] = 5
    state["first_entry_at"] = "2020-01-01T00:00:00+00:00"
    state["slot"] = CONDUCTOR
    state["generation"] = "gen-old"

    projection._work_reset_board(state)

    fresh = projection._work_start()
    assert state["entries"] == fresh["entries"], state
    assert state["first_entry_at"] == fresh["first_entry_at"], state
    assert state["slot"] == CONDUCTOR, "the slot binding is the fold's, not the board's"
    assert state["generation"] == "gen-old", "the caller assigns the new generation itself"

    # End to end: two entries for one board, then a third opening a new generation.
    unit = CrewLog.create(
        lg.KIND_SESSION, "u-conductor", owner="raymond", agent="kirocrew", slot=CONDUCTOR
    )
    old = {"slot": CONDUCTOR, "actor": "conductor", "by": CONDUCTOR, "generation": "gen-0001"}
    unit.append(
        "work/recorded", {**old, "action": "goal", "goal": "old board", "round": 1}, src="gateway"
    )
    unit.append(
        "work/recorded",
        {
            **old,
            "action": "create",
            "item_id": "it_0000aaaa",
            "title": "old",
            "event": "x",
            "event_kind": "create",
        },
        src="gateway",
    )
    before = projection.read_slot_projection(CONDUCTOR, "work").value["conductor"]
    assert before["entries"] == 2, before

    unit.append(
        "work/recorded",
        {**old, "generation": "gen-0002", "action": "goal", "goal": "new board", "round": 1},
        src="gateway",
    )

    after = projection.read_slot_projection(CONDUCTOR, "work").value["conductor"]
    assert after["goal"] == "new board", after
    assert after["entries"] == 1, after


def test_a_generationless_straggler_is_omitted_once_a_stamped_board_is_live():
    """A stamped board is live, so an entry carrying no generation is not its.

    The stamp is minted with the board and every entry of a stamped board carries it,
    so a generationless entry belongs to an earlier board under this slot -- purged,
    or dropped by the reset. The log is append-only, so it outlives that board
    forever and would resurrect its items on every fold. The mismatch branch cannot
    catch it, being reached only when the entry HAS a generation to disagree with,
    and the rebuild guard cannot either: it compares two generations and needs both
    non-empty.

    The finding's own vector is a worker baseline; a conductor create is used here
    because it materialises an item through the same line with no dependence on the
    baseline payload's shape.
    """
    from kiro_crew import crew_log as lg
    from kiro_crew.crew_log import CrewLog

    unit = CrewLog.create(
        lg.KIND_SESSION, "u-conductor", owner="raymond", agent="kirocrew", slot=CONDUCTOR
    )
    stamped = {
        "slot": CONDUCTOR,
        "actor": "conductor",
        "by": CONDUCTOR,
        "generation": "gen-0002",
    }
    unit.append(
        "work/recorded",
        {**stamped, "action": "goal", "goal": "live board", "round": 1},
        src="gateway",
    )
    unit.append(
        "work/recorded",
        {
            # No generation at all: a straggler from the board this slot held before.
            "slot": CONDUCTOR,
            "actor": "conductor",
            "by": CONDUCTOR,
            "action": "create",
            "item_id": "it_0000dead",
            "title": "the purged board's item",
            "event": "x",
            "event_kind": "create",
        },
        src="gateway",
    )

    board = projection.read_slot_projection(CONDUCTOR, "work").value
    assert board["conductor"]["goal"] == "live board", board["conductor"]
    assert board["items"] == [], board["items"]


def test_a_board_from_before_the_stamp_still_applies_its_own_entries():
    """The partner, in the other direction: over-omitting would erase legacy boards.

    A board recorded before generations existed never adopts one, so its state keeps
    an empty generation and its own generationless entries must still apply. Without
    this, a guard that omitted every generationless entry would satisfy the test
    above while silently emptying every pre-stamp board.
    """
    from kiro_crew import crew_log as lg
    from kiro_crew.crew_log import CrewLog

    legacy_slot = "chat-91-legacy"
    unit = CrewLog.create(
        lg.KIND_SESSION, "u-legacy", owner="raymond", agent="kirocrew", slot=legacy_slot
    )
    old = {"slot": legacy_slot, "actor": "conductor", "by": legacy_slot}  # no generation
    unit.append(
        "work/recorded",
        {**old, "action": "goal", "goal": "legacy board", "round": 1},
        src="gateway",
    )
    unit.append(
        "work/recorded",
        {
            **old,
            "action": "create",
            "item_id": "it_0000cafe",
            "title": "legacy item",
            "event": "x",
            "event_kind": "create",
        },
        src="gateway",
    )

    board = projection.read_slot_projection(legacy_slot, "work").value
    assert board["conductor"]["goal"] == "legacy board", board["conductor"]
    assert [item["item_id"] for item in board["items"]] == ["it_0000cafe"], board["items"]


@pytest.mark.asyncio
async def test_a_header_read_that_answers_none_once_still_carries_the_generation(
    recorded, monkeypatch
):
    """The lock-free header read answers None on a fault as well as on an absent
    ledger, and an entry appended with no generation against a STAMPED board is
    omitted by the fold -- so that answer must not reach the entry. The strict
    re-read establishes the generation and the entry carries it.

    Only the read that FEEDS the entry is faulted. The route reads the record
    earlier too, before it commits, and faulting that one would refuse before the
    entry is ever built -- which would leave this test passing for the wrong reason.
    """
    item_id = await _board()
    stamped = wl.read_conductor(CONDUCTOR)
    assert stamped is not None and stamped.generation

    real_read = wl.read_conductor
    real_report = wl.apply_worker_report
    committed: list[bool] = []
    faulted: list[str] = []

    def _after_commit(*args: Any, **kwargs: Any) -> Any:
        result = real_report(*args, **kwargs)
        committed.append(True)
        return result

    def _none_once_after_commit(slot_key: str, *, strict: bool = False) -> Any:
        if not strict and committed and not faulted:
            faulted.append(slot_key)
            return None
        return real_read(slot_key, strict=strict)

    monkeypatch.setattr(routes.work_ledger, "apply_worker_report", _after_commit)
    monkeypatch.setattr(routes.work_ledger, "read_conductor", _none_once_after_commit)
    recorded.clear()
    status, body = await _report(WORKER, {"status": "progress", "summary": "still stamped"})

    assert status == 200, body
    assert faulted == [CONDUCTOR], "the post-commit read is the one that must fault"
    # The worker's copy and the board's copy carry the same generation.
    assert [data.get("generation") for _unit, data in recorded] == [stamped.generation] * 2
    landed = wl.read_work_item(CONDUCTOR, item_id)
    assert landed is not None and landed.status == "progress"


@pytest.mark.asyncio
async def test_a_header_that_cannot_be_read_is_refused_rather_than_appended(recorded, monkeypatch):
    """When the strict re-read cannot establish the generation either, the write is
    refused and undone: an entry appended generationless here would be dropped by
    the next rebuild, which is an acknowledged report disappearing. Faulted after
    the commit, so the refusal is the generation guard's and not an earlier one's.
    """
    item_id = await _board()
    before = wl.read_work_item(CONDUCTOR, item_id)
    assert before is not None and before.status is None

    real_read = wl.read_conductor
    real_report = wl.apply_worker_report
    committed: list[bool] = []

    def _after_commit(*args: Any, **kwargs: Any) -> Any:
        result = real_report(*args, **kwargs)
        committed.append(True)
        return result

    def _unreadable_after_commit(slot_key: str, *, strict: bool = False) -> Any:
        if not committed:
            return real_read(slot_key, strict=strict)
        if strict:
            raise OSError("header momentarily unreadable")
        return None

    monkeypatch.setattr(routes.work_ledger, "apply_worker_report", _after_commit)
    monkeypatch.setattr(routes.work_ledger, "read_conductor", _unreadable_after_commit)
    recorded.clear()
    status, body = await _report(WORKER, {"status": "progress", "summary": "never appended"})

    assert (status, body["code"]) == (503, "crew_log_unrecorded"), body
    assert recorded == []
    undone = wl.read_work_item(CONDUCTOR, item_id)
    assert undone is not None and undone.status is None and undone.summary == ""


@pytest.mark.asyncio
async def test_a_board_with_no_generation_still_accepts_a_write(recorded):
    """The partner, in the other direction: a board minted before the stamp existed
    carries no generation legitimately, and its writes must still be accepted and
    appended generationless, because the fold applies such a board's own entries. A
    guard that read a MISSING generation as a fault would refuse every write on an
    upgraded deployment, which is the opposite failure from the one above.
    """
    item_id = await _board()
    header = wl.read_conductor(CONDUCTOR)
    assert header is not None
    payload = header.to_dict()
    payload["generation"] = ""
    wl._write_record(wl.conductor_dir(CONDUCTOR) / wl._CONDUCTOR_FILE, payload)
    pre_stamp = wl.read_conductor(CONDUCTOR)
    assert pre_stamp is not None and not (pre_stamp.generation or "")

    recorded.clear()
    status, body = await _report(WORKER, {"status": "progress", "summary": "legacy board"})

    assert status == 200, body
    assert [data.get("generation") for _unit, data in recorded] == [None, None]
    landed = wl.read_work_item(CONDUCTOR, item_id)
    assert landed is not None and landed.status == "progress"
