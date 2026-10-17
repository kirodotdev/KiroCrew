"""The work ledger projection across a board's lifecycle.

An abandoned append never lands, a torn header is still a board, an item stamped in the
first entry's second counts as legacy, a cancelled report drains its append, a reused
slot folds only its latest board, a bootstrap create records its lineage, and a rebuild
is all or nothing. Baselines and committed stamps survive a rebuild, which refuses a
pruned unit, a goal the record never saw, or a unit whose header cannot be read.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

import pytest
import work_ledger_projection_helpers
from work_ledger_projection_helpers import (
    _SLOTS,
    ACCEPTANCE,
    CONDUCTOR,
    WORKER,
    _board,
    _real_units,
    _record,
    _report,
    _Slot,
)

from kiro_crew import work_ledger as wl
from kiro_crew.crew_log import projection
from kiro_crew.dashboard.handlers import work_ledger as routes

# The shared fixtures, bound here by name so pytest registers them for this module.
_isolated = work_ledger_projection_helpers._isolated
recorded = work_ledger_projection_helpers.recorded


# -- an abandoned append never lands; a torn header is a board; same-second is legacy


def test_an_append_the_waiter_gave_up_on_never_lands(monkeypatch):
    """The queued job is held back until after the waiter gave up, then run as
    the writer would run it. The abandoned entry must not land: a write the
    caller was told failed would otherwise come back on the next rebuild."""
    from kiro_crew import crew_log as lg
    from kiro_crew.crew_log import CrewLog, emit

    monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
    emit.reset_caches()
    try:
        CrewLog.create(lg.KIND_SESSION, "u-slow", owner="raymond", agent="kirocrew", slot=CONDUCTOR)
        held: list[tuple[Any, Any]] = []
        real_submit = emit._submit
        monkeypatch.setattr(
            emit, "_submit", lambda job, what, sid, *a, **kw: held.append((job, kw.get("after")))
        )
        data = {
            "slot": CONDUCTOR,
            "actor": "conductor",
            "by": CONDUCTOR,
            "action": "goal",
            "goal": "late",
        }
        # flake-ok: the stubbed _submit holds the job, so the 100 ms waiter always gives up; that give-up is the subject, moved verbatim
        assert emit.on_work_recorded("u-slow", data, timeout=0.1) is False
        [(job, after)] = held
        job()  # the writer reaches the job only now
        if after is not None:
            after()
        handle = projection.open_session_log("u-slow")
        assert handle is not None
        kinds = [e.type for e in handle.iter_from(1, known=projection.KNOWN_TYPES)]
        assert kinds.count("work/recorded") == 0
        # The same entry through the real writer lands and is acknowledged.
        monkeypatch.setattr(emit, "_submit", real_submit)
        assert emit.on_work_recorded("u-slow", data) is True
    finally:
        emit.drain_for_shutdown(timeout=2.0)
        emit.reset_caches()


@pytest.mark.asyncio
async def test_a_torn_header_is_an_existing_board_and_is_not_discarded(monkeypatch):
    wl.ensure_conductor(CONDUCTOR, goal="old goal")
    old = wl.apply_conductor_action(CONDUCTOR, "create", title="old", acceptance=ACCEPTANCE)["item"]
    (wl.conductor_dir(CONDUCTOR) / "conductor.json").write_text("{not json", encoding="utf-8")
    assert wl.read_conductor(CONDUCTOR) is None
    _real_units(monkeypatch)
    monkeypatch.setattr(routes.crew_log_emit, "on_work_recorded", lambda unit, data: False)

    status, body = await _record(
        CONDUCTOR, {"action": "create", "title": "new", "acceptance": ACCEPTANCE}
    )
    assert (status, body["code"]) == (503, "crew_log_unrecorded"), body
    assert wl.read_work_item(CONDUCTOR, old.item_id) is not None


def test_an_item_stamped_in_the_first_entrys_second_counts_as_legacy():
    from datetime import datetime

    epoch = datetime.fromisoformat("2026-09-20T10:00:00+00:00")
    assert wl._predates("2026-09-20T10:00:00+00:00", epoch) is True
    assert wl._predates("2026-09-20T09:59:59+00:00", epoch) is True
    assert wl._predates("2026-09-20T10:00:01+00:00", epoch) is False
    assert wl._predates("not a stamp", epoch) is False


@pytest.mark.asyncio
async def test_a_failed_create_in_the_boards_first_second_is_still_undone(monkeypatch):
    """Existing board, first recorded write and the failed create in the SAME
    second: the legacy rule would keep the create; the compensation names it
    and it goes."""
    _real_units(monkeypatch)
    status, body = await _record(CONDUCTOR, {"action": "goal", "goal": "ship it", "round": 1})
    assert status == 200, body
    real = routes.crew_log_emit.on_work_recorded
    monkeypatch.setattr(routes.crew_log_emit, "on_work_recorded", lambda unit, data: False)
    status, body = await _record(
        CONDUCTOR, {"action": "create", "title": "same second", "acceptance": ACCEPTANCE}
    )
    assert (status, body["code"]) == (503, "crew_log_unrecorded"), body
    assert wl.read_work_item(CONDUCTOR, body["item_id"]) is None
    header = wl.read_conductor(CONDUCTOR)
    assert header is not None and header.goal == "ship it"
    monkeypatch.setattr(routes.crew_log_emit, "on_work_recorded", real)


@pytest.mark.asyncio
async def test_an_unconfirmed_report_to_a_pre_projection_item_is_undone_too(monkeypatch):
    """A v0.6-era board with no entry at all: the fold cannot judge it, but the
    undo needs no fold. The report's cache mutation is put back byte for byte."""
    wl.ensure_conductor(CONDUCTOR, goal="old goal")
    old = wl.apply_conductor_action(CONDUCTOR, "create", title="old", acceptance=ACCEPTANCE)["item"]
    _SLOTS[WORKER] = _Slot(created_by=CONDUCTOR)
    wl.apply_conductor_action(CONDUCTOR, "bind", item_id=old.item_id, worker_session_key=WORKER)
    before_item = wl.item_path(CONDUCTOR, old.item_id).read_bytes()
    before_events = wl.item_events_path(CONDUCTOR, old.item_id).read_bytes()
    _real_units(monkeypatch, worker_lands=False)

    status, body = await _report(WORKER, {"status": "progress", "summary": "unrecorded"})
    assert (status, body["code"]) == (503, "crew_log_unrecorded"), body
    assert wl.item_path(CONDUCTOR, old.item_id).read_bytes() == before_item
    assert wl.item_events_path(CONDUCTOR, old.item_id).read_bytes() == before_events


@pytest.mark.asyncio
async def test_cancelled_report_drains_its_append_before_reraising(monkeypatch):
    """Cancellation after commit waits for the record, then still propagates."""
    _real_units(monkeypatch)
    item_id = await _board()
    entered = threading.Event()
    release = threading.Event()
    append = routes.crew_log_emit.on_work_recorded

    def _blocked_append(unit: str, data: dict[str, Any]) -> bool:
        if data.get("action") == "report":
            entered.set()
            if not release.wait(2):
                raise AssertionError("the cancellation test did not release the append")
        return append(unit, data)

    monkeypatch.setattr(routes.crew_log_emit, "on_work_recorded", _blocked_append)
    pending = asyncio.create_task(
        _report(WORKER, {"status": "progress", "summary": "recorded before cancellation"})
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
    assert item is not None and (item.status, item.summary) == (
        "progress",
        "recorded before cancellation",
    )
    counts = wl.rebuild_from_projection(CONDUCTOR)
    assert counts["items"] == 1
    rebuilt = wl.read_work_item(CONDUCTOR, item_id)
    assert rebuilt is not None and (rebuilt.status, rebuilt.summary) == (
        "progress",
        "recorded before cancellation",
    )


# -- a reused slot folds only its latest board; parked keys stay within the bound


def test_a_reused_slot_folds_only_its_latest_board():
    from kiro_crew import crew_log as lg
    from kiro_crew.crew_log import CrewLog

    unit = CrewLog.create(
        lg.KIND_SESSION, "u-conductor", owner="raymond", agent="kirocrew", slot=CONDUCTOR
    )
    first = {
        "slot": CONDUCTOR,
        "actor": "conductor",
        "by": CONDUCTOR,
        "generation": "gen-aaaa0001",
    }
    unit.append(
        "work/recorded", {**first, "action": "goal", "goal": "old board", "round": 1}, src="gateway"
    )
    unit.append(
        "work/recorded",
        {
            **first,
            "action": "create",
            "item_id": "it_0000aaaa",
            "title": "old item",
            "event": "x",
            "event_kind": "create",
        },
        src="gateway",
    )
    # The board is purged and a new one opens under the same slot.
    second = {**first, "generation": "gen-bbbb0002"}
    unit.append(
        "work/recorded",
        {**second, "action": "goal", "goal": "new board", "round": 1},
        src="gateway",
    )
    unit.append(
        "work/recorded",
        {
            **second,
            "action": "create",
            "item_id": "it_0000bbbb",
            "title": "new item",
            "event": "x",
            "event_kind": "create",
        },
        src="gateway",
    )
    # A straggler from the old board, appended later (a worker's log folded after).
    unit.append(
        "work/recorded",
        {
            **first,
            "actor": "worker",
            "by": WORKER,
            "action": "report",
            "item_id": "it_0000aaaa",
            "status": "done",
            "summary": "late",
            "event": "late",
            "event_kind": "report",
        },
        src="gateway",
    )
    board = projection.read_slot_projection(CONDUCTOR, "work").value
    assert board["conductor"]["goal"] == "new board"
    assert [item["item_id"] for item in board["items"]] == ["it_0000bbbb"]
    assert board["omitted"] == 1


def test_parked_entries_keep_within_the_item_bound(monkeypatch):
    from kiro_crew import crew_log as lg
    from kiro_crew.crew_log import CrewLog

    monkeypatch.setattr(projection, "WORK_ITEM_LIMIT", 2)
    unit = CrewLog.create(
        lg.KIND_SESSION, "u-conductor", owner="raymond", agent="kirocrew", slot=CONDUCTOR
    )
    unit.append(
        "work/recorded",
        {
            "slot": CONDUCTOR,
            "actor": "conductor",
            "by": CONDUCTOR,
            "action": "goal",
            "goal": "g",
            "round": 1,
        },
        src="gateway",
    )
    for idx in range(3):  # three reports for items that never get a create
        unit.append(
            "work/recorded",
            {
                "slot": CONDUCTOR,
                "actor": "worker",
                "by": WORKER,
                "action": "report",
                "item_id": f"it_0000000{idx}",
                "status": "progress",
                "summary": "s",
                "event": "s",
                "event_kind": "report",
            },
            src="gateway",
        )
    checkpoint = projection.fold_slot_checkpoint("work", ["u-conductor"], slot=CONDUCTOR)
    assert len(checkpoint.state["parked"]) == 2
    assert checkpoint.state["omitted"] == 1


# -- lineage on a bootstrap create; the identity file's undo; all-or-nothing rebuild


@pytest.mark.asyncio
async def test_every_conductor_entry_records_the_boards_lineage(recorded):
    status, body = await _record(
        CONDUCTOR, {"action": "create", "title": "first", "acceptance": ACCEPTANCE}
    )
    assert status == 200, body
    [(unit, create)] = recorded
    # A top-level board: depth 0 is recorded; parent_item is None and so absent.
    assert (create["action"], create["depth"]) == ("create", 0)
    assert "parent_item" not in create
    status, body = await _record(
        CONDUCTOR, {"action": "create", "title": "second", "acceptance": ACCEPTANCE}
    )
    assert status == 200, body
    # Lineage is board identity: every conductor entry carries it.
    second = recorded[-1][1]
    assert second["depth"] == 0


@pytest.mark.asyncio
async def test_a_board_born_in_a_failed_request_leaves_no_identity_file(monkeypatch):
    _real_units(monkeypatch)
    monkeypatch.setattr(routes.crew_log_emit, "on_work_recorded", lambda unit, data: False)
    status, body = await _record(CONDUCTOR, {"action": "goal", "goal": "ship it", "round": 1})
    assert (status, body["code"]) == (503, "crew_log_unrecorded"), body
    board = wl.conductor_dir(CONDUCTOR)
    assert not (board / "conductor.json").exists() and not (board / "slot_key").exists()


def _cache_files(slot: str) -> dict[str, bytes]:
    """The board's records and event logs by name; lock files are not content."""
    return {
        p.name: p.read_bytes()
        for p in wl.conductor_dir(slot).rglob("*")
        if p.is_file() and not p.name.endswith(".lock")
    }


def test_a_rebuild_that_fails_part_way_leaves_the_cache_as_it_was(monkeypatch):
    from kiro_crew import crew_log as lg
    from kiro_crew.crew_log import CrewLog

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
            "item_id": "it_0000abcd",
            "title": "recorded",
            "event": "recorded",
            "event_kind": "create",
        },
        src="gateway",
    )
    # The cache holds something else entirely: a different goal and a stray item.
    wl.ensure_conductor(CONDUCTOR, goal="cache goal")
    stray = wl.item_path(CONDUCTOR, "it_0000dead")
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_text(json.dumps({"item_id": "it_0000dead", "title": "stray"}), encoding="utf-8")
    before = _cache_files(CONDUCTOR)

    real_write = wl._write_record
    calls = {"n": 0}

    def _failing_write(path, payload):
        calls["n"] += 1
        if calls["n"] == 2:  # the item went through; the header write after it fails
            raise OSError("disk full")
        return real_write(path, payload)

    monkeypatch.setattr(wl, "_write_record", _failing_write)
    with pytest.raises(OSError):
        wl.rebuild_from_projection(CONDUCTOR)
    assert _cache_files(CONDUCTOR) == before


def test_a_generationless_board_is_dropped_when_a_stamped_one_opens_the_slot():
    from kiro_crew import crew_log as lg
    from kiro_crew.crew_log import CrewLog

    unit = CrewLog.create(
        lg.KIND_SESSION, "u-conductor", owner="raymond", agent="kirocrew", slot=CONDUCTOR
    )
    old = {"slot": CONDUCTOR, "actor": "conductor", "by": CONDUCTOR}  # no generation at all
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
    new = {**old, "generation": "gen-0001"}
    unit.append(
        "work/recorded", {**new, "action": "goal", "goal": "new board", "round": 1}, src="gateway"
    )
    board = projection.read_slot_projection(CONDUCTOR, "work").value
    assert board["conductor"]["goal"] == "new board"
    assert board["items"] == []


@pytest.mark.asyncio
async def test_an_unconfirmed_bind_spelled_with_the_dashboard_prefix_is_undone(monkeypatch):
    """The store folds `dashboard_chat-X` to `chat-X` before writing the binding;
    the undo must name the folded path or the binding would survive."""
    _real_units(monkeypatch)
    await _board()
    status, body = await _record(
        CONDUCTOR, {"action": "create", "title": "unbound", "acceptance": ACCEPTANCE}
    )
    assert status == 200, body
    item_id = body["item"]["item_id"]
    _SLOTS["chat-9-other"] = _Slot(created_by=CONDUCTOR)
    monkeypatch.setattr(routes.crew_log_emit, "on_work_recorded", lambda unit, data: False)
    status, body = await _record(
        CONDUCTOR,
        {"action": "bind", "item_id": item_id, "worker_session_key": "dashboard_chat-9-other"},
    )
    assert (status, body["code"]) == (503, "crew_log_unrecorded"), body
    assert wl.read_binding("chat-9-other") is None
    assert wl.read_binding("dashboard_chat-9-other") is None


def test_a_rebuild_recreates_the_identity_breadcrumb():
    from kiro_crew import crew_log as lg
    from kiro_crew.crew_log import CrewLog

    unit = CrewLog.create(
        lg.KIND_SESSION, "u-conductor", owner="raymond", agent="kirocrew", slot=CONDUCTOR
    )
    mine = {"slot": CONDUCTOR, "actor": "conductor", "by": CONDUCTOR, "generation": "gen-0001"}
    unit.append(
        "work/recorded", {**mine, "action": "goal", "goal": "ship it", "round": 1}, src="gateway"
    )
    assert not (wl.conductor_dir(CONDUCTOR) / "slot_key").exists()
    wl.rebuild_from_projection(CONDUCTOR)
    assert (wl.conductor_dir(CONDUCTOR) / "slot_key").read_text(
        encoding="utf-8"
    ) == CONDUCTOR + "\n"
    assert wl.read_conductor(CONDUCTOR) is not None


# -- baselines; committed stamps; an unreadable unit refuses the rebuild --------


@pytest.mark.asyncio
async def test_a_legacy_items_first_recorded_mutation_lets_a_lost_file_rebuild(monkeypatch):
    """A pre-projection item has no create entry. Its first recorded report carries
    the whole item as a baseline; after the item file is lost, the rebuild brings
    it back with its title, acceptance and the report."""
    wl.ensure_conductor(CONDUCTOR, goal="old goal")
    old = wl.apply_conductor_action(CONDUCTOR, "create", title="old", acceptance=ACCEPTANCE)["item"]
    _SLOTS[WORKER] = _Slot(created_by=CONDUCTOR)
    wl.apply_conductor_action(CONDUCTOR, "bind", item_id=old.item_id, worker_session_key=WORKER)
    _real_units(monkeypatch)

    status, body = await _report(WORKER, {"status": "progress", "summary": "first recorded"})
    assert status == 200, body
    assert wl.read_work_item(CONDUCTOR, old.item_id).recorded_at

    wl.item_path(CONDUCTOR, old.item_id).unlink()
    wl.item_events_path(CONDUCTOR, old.item_id).unlink()
    counts = wl.rebuild_from_projection(CONDUCTOR)
    assert counts["items"] == 1, counts
    back = wl.read_work_item(CONDUCTOR, old.item_id)
    assert back is not None
    assert (back.title, back.acceptance, back.worker_session_key) == ("old", ACCEPTANCE, WORKER)
    assert (back.status, back.summary, back.created_at) == (
        "progress",
        "first recorded",
        old.created_at,
    )


@pytest.mark.asyncio
async def test_a_legacy_workers_item_rebuilds_after_the_cache_and_bindings_are_lost(
    monkeypatch,
):
    """A bind from before the projection was never recorded, so the fold's own
    worker discovery (the conductor's recorded ``bind`` entries) cannot reach the
    worker, and once the cache is lost neither can the binding files. The worker's
    own log still names the board in its baseline report; the rebuild must find
    the worker through that, or the recorded item is silently not restored."""
    import shutil

    wl.ensure_conductor(CONDUCTOR, goal="old goal")
    old = wl.apply_conductor_action(CONDUCTOR, "create", title="old", acceptance=ACCEPTANCE)["item"]
    _SLOTS[WORKER] = _Slot(created_by=CONDUCTOR)
    wl.apply_conductor_action(CONDUCTOR, "bind", item_id=old.item_id, worker_session_key=WORKER)
    _real_units(monkeypatch)

    status, body = await _report(WORKER, {"status": "progress", "summary": "first recorded"})
    assert status == 200, body

    # The whole work-ledger tree goes: the board's cache and every binding file.
    shutil.rmtree(wl.conductor_dir(CONDUCTOR))
    if wl.bindings_dir().is_dir():
        shutil.rmtree(wl.bindings_dir())

    counts = wl.rebuild_from_projection(CONDUCTOR)
    assert counts["items"] == 1, counts
    back = wl.read_work_item(CONDUCTOR, old.item_id)
    assert back is not None
    assert (back.title, back.acceptance, back.worker_session_key) == ("old", ACCEPTANCE, WORKER)
    assert (back.status, back.summary) == ("progress", "first recorded")
    assert back.recorded_at


@pytest.mark.asyncio
async def test_a_legacy_boards_round_creation_and_report_stamps_survive_a_rebuild(monkeypatch):
    """A board from before the projection has a round, a creation stamp and items
    with reports the record never saw. Its first recorded conductor mutation
    carries them in the baseline under the board's own names, so a rebuild after
    the cache is lost neither zeroes the round, nor dates the board from the first
    entry, nor clears the item's last report."""
    import shutil

    wl.ensure_conductor(CONDUCTOR, goal="old goal")
    wl.apply_conductor_action(CONDUCTOR, "goal", goal="old goal", round_number=3)
    old = wl.apply_conductor_action(CONDUCTOR, "create", title="old", acceptance=ACCEPTANCE)["item"]
    _SLOTS[WORKER] = _Slot(created_by=CONDUCTOR)
    wl.apply_conductor_action(CONDUCTOR, "bind", item_id=old.item_id, worker_session_key=WORKER)
    wl.apply_worker_report(CONDUCTOR, old.item_id, status="progress", summary="before the record")
    header = wl.read_conductor(CONDUCTOR)
    reported = wl.read_work_item(CONDUCTOR, old.item_id)
    assert header.round == 3 and header.created_at and reported.last_report_at
    _real_units(monkeypatch)

    status, body = await _record(
        CONDUCTOR, {"action": "decide", "item_id": old.item_id, "decision": "carry on"}
    )
    assert status == 200, body

    shutil.rmtree(wl.conductor_dir(CONDUCTOR))
    counts = wl.rebuild_from_projection(CONDUCTOR)
    assert counts["items"] == 1, counts
    rebuilt = wl.read_conductor(CONDUCTOR)
    assert (rebuilt.round, rebuilt.created_at, rebuilt.goal) == (3, header.created_at, "old goal")
    back = wl.read_work_item(CONDUCTOR, old.item_id)
    assert (back.last_report_at, back.status, back.summary) == (
        reported.last_report_at,
        "progress",
        "before the record",
    )
    assert back.decision == "carry on"


@pytest.mark.asyncio
async def test_a_pruned_newest_unit_is_detected_when_the_cache_holds_its_cap_of_events(
    monkeypatch,
):
    """The store keeps an item's newest ``MAX_EVENTS_PER_ITEM`` events, and the fold
    keeps the same, so once the cache is at its cap a count comparison says nothing.
    The newest cached event is then the tell: a fold without it lost the unit that
    recorded it, and rebuilding from it would overwrite the latest decision."""
    monkeypatch.setattr(wl, "MAX_EVENTS_PER_ITEM", 3)
    _real_units(monkeypatch)
    item_id = await _board()
    for n in range(4):
        status, body = await _record(
            CONDUCTOR, {"action": "decide", "item_id": item_id, "decision": f"round {n}"}
        )
        assert status == 200, body
    assert len(wl.read_events(CONDUCTOR, item_id)) == 3

    # The newest decision lands in the cache; its entry never reaches the record.
    monkeypatch.setattr(routes.crew_log_emit, "on_work_recorded", lambda unit, data: True)
    status, body = await _record(
        CONDUCTOR, {"action": "decide", "item_id": item_id, "decision": "newest, lost"}
    )
    assert status == 200, body
    assert len(wl.read_events(CONDUCTOR, item_id)) == 3

    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.rebuild_from_projection(CONDUCTOR)
    assert caught.value.code == wl.CODE_CREW_LOG_INCOMPLETE
    assert wl.read_work_item(CONDUCTOR, item_id).decision == "newest, lost"


@pytest.mark.asyncio
async def test_a_goal_the_record_never_saw_refuses_the_rebuild(monkeypatch):
    """The header has the same two-step write as an item. A goal committed to the
    cache whose entry never landed (the gateway died between commit and append)
    leaves the cache one goal ahead of the record; a later recorded write then
    makes the fold look complete. A rebuild must refuse rather than put the older
    goal and round back, and the refusal must name the item-free remedy."""
    _real_units(monkeypatch)
    item_id = await _board()  # goal (recorded), create, bind
    header = wl.read_conductor(CONDUCTOR)
    assert header.recorded_at and header.goal_version == 1

    # The second goal lands in the cache; its entry never reaches the record.
    landed = routes.crew_log_emit.on_work_recorded
    monkeypatch.setattr(routes.crew_log_emit, "on_work_recorded", lambda unit, data: True)
    status, body = await _record(
        CONDUCTOR, {"action": "goal", "goal": "ship it, then rest", "round": 2}
    )
    assert status == 200, body
    monkeypatch.setattr(routes.crew_log_emit, "on_work_recorded", landed)
    after = wl.read_conductor(CONDUCTOR)
    assert (after.goal, after.round, after.goal_version) == ("ship it, then rest", 2, 2)

    # A later recorded write: the fold has entries and looks whole.
    status, body = await _record(
        CONDUCTOR, {"action": "decide", "item_id": item_id, "decision": "go"}
    )
    assert status == 200, body

    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.rebuild_from_projection(CONDUCTOR)
    assert caught.value.code == wl.CODE_CREW_LOG_INCOMPLETE
    assert "action=goal" in str(caught.value)
    kept = wl.read_conductor(CONDUCTOR)
    assert (kept.goal, kept.round) == ("ship it, then rest", 2)

    # The named remedy: record the goal again, and the rebuild is allowed.
    status, body = await _record(
        CONDUCTOR, {"action": "goal", "goal": "ship it, then rest", "round": 2}
    )
    assert status == 200, body
    wl.rebuild_from_projection(CONDUCTOR)
    rebuilt = wl.read_conductor(CONDUCTOR)
    assert (rebuilt.goal, rebuilt.round) == ("ship it, then rest", 2)
    assert rebuilt.recorded_at


@pytest.mark.asyncio
async def test_a_rebuild_never_reclaims_a_worker_another_board_now_holds(monkeypatch):
    """``close`` leaves the finished item naming its worker, and the store then
    lets another board bind that worker. A rebuild of the first board must not
    write the worker's binding back to its own terminal item -- that would route
    the second board's worker to a finished item, silently. A binding that still
    points at the terminal item (the worker never moved on) is kept."""
    _real_units(monkeypatch)
    item_id = await _board()
    status, body = await _report(
        WORKER, {"status": "done", "summary": "finished", "artifacts": {"commit": "abc1234"}}
    )
    assert status == 200, body
    status, body = await _record(
        CONDUCTOR, {"action": "close", "item_id": item_id, "state": "accepted"}
    )
    assert status == 200, body
    assert wl.read_work_item(CONDUCTOR, item_id).is_terminal

    # Never moved on: the rebuild keeps the binding where it is.
    wl.rebuild_from_projection(CONDUCTOR)
    assert wl.read_binding(WORKER) == (CONDUCTOR, item_id)

    # Another board binds the worker (permitted: this board's item is terminal).
    other = "chat-9-other-conductor"
    _SLOTS[other] = _Slot(created_by="")
    wl.ensure_conductor(other, goal="next goal")
    next_item = wl.apply_conductor_action(other, "create", title="next", acceptance=ACCEPTANCE)[
        "item"
    ]
    wl.apply_conductor_action(other, "bind", item_id=next_item.item_id, worker_session_key=WORKER)
    assert wl.read_binding(WORKER) == (other, next_item.item_id)

    wl.rebuild_from_projection(CONDUCTOR)
    assert wl.read_binding(WORKER) == (other, next_item.item_id)
    # This board's finished item still names the worker that finished it.
    assert wl.read_work_item(CONDUCTOR, item_id).worker_session_key == WORKER


@pytest.mark.asyncio
async def test_a_baseline_born_items_later_mutation_missing_from_the_record_refuses_the_rebuild(
    monkeypatch,
):
    """An item from before the projection is recorded whole by its first recorded
    mutation. A LATER mutation whose entry is missing from the record (its unit
    pruned, or an append the cache saw land but the log did not) must still be
    detected: the cache's events from that first recorded mutation on are the
    record's to explain, and a rebuild that folded fewer would overwrite the
    cached decision with the baseline's."""
    wl.ensure_conductor(CONDUCTOR, goal="old goal")
    old = wl.apply_conductor_action(CONDUCTOR, "create", title="old", acceptance=ACCEPTANCE)["item"]
    _SLOTS[WORKER] = _Slot(created_by=CONDUCTOR)
    wl.apply_conductor_action(CONDUCTOR, "bind", item_id=old.item_id, worker_session_key=WORKER)
    _real_units(monkeypatch)

    status, body = await _record(
        CONDUCTOR, {"action": "decide", "item_id": old.item_id, "decision": "first, recorded"}
    )
    assert status == 200, body
    assert wl.read_work_item(CONDUCTOR, old.item_id).recorded_at

    # The second decision lands in the cache; its entry never reaches the record.
    landed = routes.crew_log_emit.on_work_recorded
    monkeypatch.setattr(routes.crew_log_emit, "on_work_recorded", lambda unit, data: True)
    status, body = await _record(
        CONDUCTOR, {"action": "decide", "item_id": old.item_id, "decision": "second, lost"}
    )
    assert status == 200, body
    monkeypatch.setattr(routes.crew_log_emit, "on_work_recorded", landed)
    assert wl.read_work_item(CONDUCTOR, old.item_id).decision == "second, lost"

    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.rebuild_from_projection(CONDUCTOR)
    assert caught.value.code == wl.CODE_CREW_LOG_INCOMPLETE
    assert old.item_id in str(caught.value)
    # Refused, so the cache still holds the later decision.
    assert wl.read_work_item(CONDUCTOR, old.item_id).decision == "second, lost"


@pytest.mark.asyncio
async def test_a_rebuild_reproduces_the_stores_stamps_and_event_ids(monkeypatch):
    _real_units(monkeypatch)
    item_id = await _board()
    status, body = await _report(WORKER, {"status": "progress", "summary": "half way", "pr": 12})
    assert status == 200, body
    before_item = wl.read_work_item(CONDUCTOR, item_id)
    before_events = wl.item_events_path(CONDUCTOR, item_id).read_text(encoding="utf-8")

    wl.rebuild_from_projection(CONDUCTOR)
    after_item = wl.read_work_item(CONDUCTOR, item_id)
    assert (after_item.created_at, after_item.last_report_at) == (
        before_item.created_at,
        before_item.last_report_at,
    )
    after_events = wl.item_events_path(CONDUCTOR, item_id).read_text(encoding="utf-8")
    assert [json.loads(line)["id"] for line in after_events.splitlines()] == [
        json.loads(line)["id"] for line in before_events.splitlines()
    ]
    assert [json.loads(line)["ts"] for line in after_events.splitlines()] == [
        json.loads(line)["ts"] for line in before_events.splitlines()
    ]


def test_fold_fallback_event_id_matches_the_stores_report_id():
    """A legacy entry without an explicit id uses the store's exact formula."""
    from kiro_crew import crew_log as lg
    from kiro_crew.crew_log import CrewLog

    wl.ensure_conductor(CONDUCTOR, goal="ship it")
    created = wl.apply_conductor_action(
        CONDUCTOR, "create", title="item one", acceptance=ACCEPTANCE
    )
    item_id = created["item"].item_id
    stored = wl.apply_worker_report(
        CONDUCTOR,
        item_id,
        status="blocked",
        summary="waiting on owner",
    )["event"]

    unit = CrewLog.create(
        lg.KIND_SESSION, "u-conductor", owner="raymond", agent="kirocrew", slot=CONDUCTOR
    )
    mine = {"slot": CONDUCTOR, "actor": "conductor", "by": CONDUCTOR}
    unit.append(
        "work/recorded",
        {
            **mine,
            "action": "create",
            "item_id": item_id,
            "title": "item one",
            "event": "item one",
            "event_kind": "create",
        },
        src="gateway",
    )
    unit.append(
        "work/recorded",
        {
            "slot": CONDUCTOR,
            "actor": "worker",
            "by": WORKER,
            "action": "report",
            "item_id": item_id,
            "status": "blocked",
            "summary": "waiting on owner",
            "event": "waiting on owner",
            "event_kind": "report",
            "event_ts": stored.ts,
        },
        src="gateway",
    )

    folded = projection.read_slot_projection(CONDUCTOR, "work").value
    [item] = folded["items"]
    report = item["events"][-1]
    assert (report["kind"], report["status"], report["text"]) == (
        "report",
        "blocked",
        "waiting on owner",
    )
    assert report["id"] == stored.id


def test_a_rebuild_refuses_while_a_units_header_cannot_be_read():
    from kiro_crew import crew_log as lg
    from kiro_crew.crew_log import CrewLog, store

    unit = CrewLog.create(
        lg.KIND_SESSION, "u-conductor", owner="raymond", agent="kirocrew", slot=CONDUCTOR
    )
    unit.append(
        "work/recorded",
        {
            "slot": CONDUCTOR,
            "actor": "conductor",
            "by": CONDUCTOR,
            "action": "goal",
            "goal": "g",
            "round": 1,
        },
        src="gateway",
    )
    # A unit whose header is unreadable: an empty directory beside the real one.
    root = store.crew_log_dir(lg.KIND_SESSION, "u-conductor").parent
    (root / "u-torn").mkdir()
    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.rebuild_from_projection(CONDUCTOR)
    assert caught.value.code == "crew_log_incomplete"
