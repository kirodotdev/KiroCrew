"""A board's folds read the board's OWN units, and the gateway relays what they need.

``work`` and ``workstreams`` are folded from the conductor's own crew-log units only. A
bound worker's report is written into the conductor's log by the report route, a
worker's charges are relayed there by ``work_relay`` as ``work/charged``, and a nested
board's records are relayed up into its parent's log. Nothing walks a bind to find a
worker's log, so an advance costs the entries that arrived and no more.

Each test runs the real crew-log writer on a temp data home.
"""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog
from kiro_crew.crew_log import eager as crew_log_eager
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.crew_log import work_relay
from kiro_crew.crew_log.emit_runtime import _write as emit_write

LEAD = "chat-own-units-lead"
WORKER = "chat-own-units-worker"
SUB_WORKER = "chat-own-units-sub-worker"
LEAD_UNIT = "u-lead"
WORKER_UNIT = "u-worker"
SUB_WORKER_UNIT = "u-sub-worker"


@pytest.fixture(autouse=True)
def _live_crew_log(tmp_path):
    """A real crew log on its own data home, the eager folder and the relay reset."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
        patch.setenv("KIROCREW_CREW_LOG", "1")
        crew_log_emit.reset_caches()
        crew_log_eager.stop_for_tests()
        crew_log.forget_slot_folds()
        work_relay.forget_for_tests()
        yield patch
        crew_log_emit.flush()
        crew_log_eager.stop_for_tests()
        crew_log.forget_slot_folds()
        work_relay.forget_for_tests()
        crew_log_emit.reset_caches()


def _unit(unit: str, slot: str) -> CrewLog:
    return CrewLog.create(lg.KIND_SESSION, unit, owner="owner", agent="lead", slot=slot)


def _record(unit: str, **fields: Any) -> None:
    """One ``work/recorded`` through the real append path, settled."""
    assert crew_log_emit.on_work_recorded(unit, dict(fields), timeout=5.0) is True
    _settle()


def _settle() -> None:
    crew_log_emit.flush()
    assert crew_log_eager.drain(timeout=10.0)
    # A relay queues its copy from inside the append it copies; one more round lands it.
    crew_log_emit.flush()
    assert crew_log_eager.drain(timeout=10.0)


def _lead_board(*, item: str = "it_a", bind: str | None = WORKER) -> None:
    base = {"slot": LEAD, "actor": "conductor", "by": LEAD}
    _unit(LEAD_UNIT, LEAD)
    _record(LEAD_UNIT, **base, action="goal", goal="ship", round=1)
    _record(
        LEAD_UNIT,
        **base,
        action="create",
        item_id=item,
        title="the task",
        acceptance={"kind": "human_approval"},
    )
    if bind:
        _record(LEAD_UNIT, **base, action="bind", item_id=item, worker_session_key=bind)


def _report(unit: str, *, summary: str, status: str = "progress") -> dict[str, Any]:
    return {
        "slot": LEAD,
        "actor": "worker",
        "by": WORKER,
        "action": "report",
        "item_id": "it_a",
        "status": status,
        "summary": summary,
    }


def _state(name: str) -> dict[str, Any]:
    """*name*'s raw fold state for the lead's board, through the warm cell."""
    from kiro_crew.crew_log.projection_readers import _slot_units_for_fold

    units = _slot_units_for_fold(LEAD, name)
    return crew_log.fold_slot_warm(name, units, slot=LEAD).state


def _item(name: str = "work") -> dict[str, Any]:
    [item] = crew_log.read_slot_projection(LEAD, name).value["items"]
    return item


class _Walked:
    """How many entries every crew-log walk yielded inside the block."""

    def __init__(self, patch: pytest.MonkeyPatch) -> None:
        self.count = 0
        real = CrewLog.iter_from

        def counted(handle, *args, **kwargs):
            for entry in real(handle, *args, **kwargs):
                self.count += 1
                yield entry

        patch.setattr(CrewLog, "iter_from", counted)

    def __enter__(self) -> "_Walked":
        self.count = 0
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


# --------------------------------------------------------------------------- #
# work: the board's own units, no migration
# --------------------------------------------------------------------------- #


def test_an_open_bound_item_with_no_board_side_report_waits_for_its_next_report():
    """A report only in the worker's log is never read; the item says it is waiting."""
    _lead_board()
    worker = _unit(WORKER_UNIT, WORKER)
    worker.append("work/recorded", _report(WORKER_UNIT, summary="only mine"), src="gateway")
    item = _item()
    assert item["status"] is None
    assert item["summary"] == crew_log.WORK_AWAITING_REPORT


def test_a_closed_item_with_no_report_keeps_its_state_and_shows_no_worker_summary():
    _lead_board()
    _record(
        LEAD_UNIT,
        slot=LEAD,
        actor="conductor",
        by=LEAD,
        action="close",
        item_id="it_a",
        state="abandoned",
    )
    item = _item()
    assert item["state"] == "abandoned"
    assert item["status"] is None
    assert item["summary"] == ""


def test_one_report_is_read_from_the_boards_log_without_refolding(_live_crew_log):
    """The board's copy lands in the conductor's log; the next read takes that entry."""
    _lead_board()
    _unit(WORKER_UNIT, WORKER)
    lead = CrewLog.open(lg.KIND_SESSION, LEAD_UNIT)
    lead.append(
        "turn/started", {"turn": 1, "actor": "user", "depth": 0}, src="gateway"
    )  # history the warm read must not walk again
    assert _item()["summary"] == crew_log.WORK_AWAITING_REPORT

    _record(WORKER_UNIT, **_report(WORKER_UNIT, summary="half way"))
    with crew_log_eager.paused():
        assert crew_log_emit.on_work_recorded(
            LEAD_UNIT, _report(LEAD_UNIT, summary="half way"), timeout=5.0
        )
        with _Walked(_live_crew_log) as walked:
            item = _item()
    assert (item["status"], item["summary"]) == ("progress", "half way")
    assert walked.count == 1, f"one new report walked {walked.count} entries"


@pytest.mark.parametrize("name", ["work", "workstreams"])
def test_an_eager_advance_with_no_new_entry_reads_no_old_entry(_live_crew_log, name):
    _lead_board()
    _record(LEAD_UNIT, **_report(LEAD_UNIT, summary="warm"))
    crew_log.read_slot_projection(LEAD, name)
    with _Walked(_live_crew_log) as walked:
        crew_log_eager._advance(LEAD, name)
    assert walked.count == 0, f"an idle {name} advance walked {walked.count} entries"


# --------------------------------------------------------------------------- #
# workstreams: charges and nested boards relayed into the board's own log
# --------------------------------------------------------------------------- #


def _turn(unit: str, turn: int, credits: float) -> None:
    emit_write(unit, "turn/started", {"turn": turn, "actor": "user", "depth": 0})
    emit_write(
        unit,
        "turn/completed",
        {
            "turn": turn,
            "stop_reason": "end_turn",
            "depth": 0,
            "duration_ms": 10,
            "model": "opus",
            "provider": "kiro",
            "credits": credits,
            "tokens": {"input": 5, "output": 1, "cache_read": 0, "cache_write": 0},
        },
    )
    _settle()


def _bindings(patch: pytest.MonkeyPatch, table: dict[str, str]) -> None:
    patch.setattr(work_relay, "_bound_board", lambda worker: table.get(worker, ""))


def _lead_entries(unit: str = LEAD_UNIT) -> "list[Any]":
    return [
        e
        for e in CrewLog.open(lg.KIND_SESSION, unit).iter_from(1, known=crew_log.KNOWN_TYPES)
        if e.type in ("work/charged", "work/recorded")
    ]


def test_a_workers_charge_is_relayed_into_the_board_and_costs_its_task(_live_crew_log):
    _lead_board()
    _unit(WORKER_UNIT, WORKER)
    _bindings(_live_crew_log, {WORKER: LEAD})
    _turn(WORKER_UNIT, 1, 2.5)
    charges = [e for e in _lead_entries() if e.type == "work/charged"]
    assert [(c.data["by"], c.data["credits"]) for c in charges] == [(WORKER, 2.5)]
    state = _state("workstreams")
    assert state["spend"][WORKER]["credits"] == 2.5


def test_a_closer_relayed_twice_is_billed_once(_live_crew_log):
    _lead_board()
    _unit(WORKER_UNIT, WORKER)
    _bindings(_live_crew_log, {WORKER: LEAD})
    _turn(WORKER_UNIT, 1, 4.0)
    [charge] = [e for e in _lead_entries() if e.type == "work/charged"]
    # A second copy of the same closer, as a retry or a second path would leave it.
    emit_write(LEAD_UNIT, "work/charged", dict(charge.data), src="gateway")
    _settle()
    state = _state("workstreams")
    assert state["spend"][WORKER]["credits"] == 4.0
    assert state["spend"][WORKER]["reported"] == 1


def test_closers_with_a_long_shared_session_id_are_billed_apart():
    """Two closers whose ids share a 199-char session id and differ in seq are two
    charges; the same closer met twice is still one."""
    _lead_board()
    session = "s" * 199
    for seq, credits in ((7, 1.5), (8, 2.5), (8, 2.5)):
        charge = {
            "slot": LEAD,
            "by": WORKER,
            "source": "turn",
            "closer": f"{session}:{seq}",
            "credits": credits,
            "duration_ms": 1,
        }
        emit_write(LEAD_UNIT, "work/charged", charge, src="gateway")
    _settle()
    state = _state("workstreams")
    assert state["spend"][WORKER]["credits"] == 4.0
    assert state["spend"][WORKER]["reported"] == 2


def test_a_nested_boards_records_and_charges_reach_the_parent_board(_live_crew_log):
    """The worker conducts a sub-board: its records and its own worker's charges are
    relayed up, so the lead's report shows and costs the nested board."""
    _lead_board()
    _unit(WORKER_UNIT, WORKER)
    _unit(SUB_WORKER_UNIT, SUB_WORKER)
    _bindings(_live_crew_log, {WORKER: LEAD, SUB_WORKER: WORKER})
    sub = {"slot": WORKER, "actor": "conductor", "by": WORKER}
    _record(WORKER_UNIT, **sub, action="goal", goal="the sub-goal", round=1, depth=1)
    _record(
        WORKER_UNIT,
        **sub,
        action="create",
        item_id="it_s",
        title="sub task",
        acceptance={"kind": "human_approval"},
    )
    _record(WORKER_UNIT, **sub, action="bind", item_id="it_s", worker_session_key=SUB_WORKER)
    _turn(SUB_WORKER_UNIT, 1, 1.5)

    relayed = [
        e for e in _lead_entries() if e.data.get("slot") == WORKER or e.type == "work/charged"
    ]
    assert {e.data.get("action") for e in relayed if e.type == "work/recorded"} == {
        "goal",
        "create",
        "bind",
    }
    assert [e.data["by"] for e in relayed if e.type == "work/charged"] == [SUB_WORKER]
    state = _state("workstreams")
    assert state["spend"][SUB_WORKER]["credits"] == 1.5
    goals = {board["goal"] for board in state["boards"].values()}
    assert "the sub-goal" in goals


# --------------------------------------------------------------------------- #
# the rebuild and the relay's edges
# --------------------------------------------------------------------------- #


def test_a_report_in_both_logs_is_applied_once_by_a_joined_fold():
    """The operator's rebuild joins worker logs, where each new report also lives.

    Each copy carries the same store event id, so the joined fold applies it once --
    and an earlier report met again after a later one does not put its status back.
    """
    _lead_board()
    _unit(WORKER_UNIT, WORKER)
    first = {**_report(LEAD_UNIT, summary="first"), "event_id": "ev-1", "event": "first"}
    later = {
        **_report(LEAD_UNIT, summary="later", status="blocked"),
        "board_copy": True,
        "event_id": "ev-2",
        "event": "later",
        "event_kind": "report",
        "reason": "needs_human",
    }
    first["event_kind"] = "report"
    _record(LEAD_UNIT, **first, board_copy=True)
    _record(LEAD_UNIT, **later)
    _record(WORKER_UNIT, **first, mirror=True)  # the later report's mirror never landed
    joined = crew_log.read_slot_projection(LEAD, "work", also_slots=[WORKER]).value
    [item] = joined["items"]
    assert (item["status"], item["summary"]) == ("blocked", "later")
    assert [event["id"] for event in item["events"]].count("ev-1") == 1
    assert "_board_reported" not in item


def test_a_legacy_worker_only_report_never_undoes_a_newer_one_in_the_rebuild():
    """The rebuild folds the board's log, then the worker's. A report from before board
    copies lives only in the worker's log and is met AFTER a newer board copy; it keeps
    its event line and leaves the board copy's fields standing."""
    _lead_board()
    _unit(WORKER_UNIT, WORKER)
    legacy = {
        **_report(WORKER_UNIT, summary="old", status="blocked"),
        "event_id": "ev-old",
        "event": "old",
        "event_kind": "report",
    }
    newer = {
        **_report(LEAD_UNIT, summary="new"),
        "event_id": "ev-new",
        "event": "new",
        "event_kind": "report",
    }
    _record(WORKER_UNIT, **legacy)
    _record(LEAD_UNIT, **newer, board_copy=True)
    _record(WORKER_UNIT, **newer, mirror=True)
    joined = crew_log.read_slot_projection(LEAD, "work", also_slots=[WORKER]).value
    [item] = joined["items"]
    assert (item["status"], item["summary"]) == ("progress", "new")
    assert {"ev-old", "ev-new"} <= {event["id"] for event in item["events"]}
    assert "_board_reported" not in item


def test_a_fallback_report_then_a_board_copy_rebuild_to_the_newer_report():
    """With no board unit to take it, a report's worker copy is the only record and is
    left unmarked. The rebuild keeps its fields while it is the only report, and lets a
    later board copy for the same item replace them, keeping both event lines."""
    _lead_board()
    _unit(WORKER_UNIT, WORKER)
    fallback = {
        **_report(WORKER_UNIT, summary="fallback", status="blocked"),
        "event_id": "ev-fallback",
        "event": "fallback",
        "event_kind": "report",
        "reason": "needs_human",
    }
    _record(WORKER_UNIT, **fallback)
    [item] = crew_log.read_slot_projection(LEAD, "work", also_slots=[WORKER]).value["items"]
    assert (item["status"], item["summary"], item["reason"]) == (
        "blocked",
        "fallback",
        "needs_human",
    )
    later = {
        **_report(LEAD_UNIT, summary="later"),
        "event_id": "ev-later",
        "event": "later",
        "event_kind": "report",
    }
    _record(LEAD_UNIT, **later, board_copy=True)
    _record(WORKER_UNIT, **later, mirror=True)
    [item] = crew_log.read_slot_projection(LEAD, "work", also_slots=[WORKER]).value["items"]
    assert (item["status"], item["summary"], item["reason"]) == ("progress", "later", None)
    ids = [event["id"] for event in item["events"]]
    assert ids.count("ev-fallback") == 1 and ids.count("ev-later") == 1


def test_board_copies_apply_in_log_order_whatever_their_ids_and_stamps():
    """Two board copies that share an event id (same second, same text) both apply, and
    a later one whose wall-clock stamp stepped backward still wins: the board's log is
    in writer order, and no id or clock overrides it."""
    _lead_board()
    _unit(WORKER_UNIT, WORKER)
    base = {"event_id": "ev-same", "event": "same", "event_kind": "report", "board_copy": True}
    _record(
        LEAD_UNIT,
        **_report(LEAD_UNIT, summary="same"),
        **base,
        pr=1,
        last_report_at="2026-01-02T00:00:00Z",
    )
    _record(
        LEAD_UNIT,
        **_report(LEAD_UNIT, summary="same", status="done"),
        **base,
        pr=2,
        last_report_at="2026-01-01T00:00:00Z",
    )
    for also in ((), (WORKER,)):
        [item] = crew_log.read_slot_projection(LEAD, "work", also_slots=list(also)).value["items"]
        assert (item["status"], item["pr"]) == ("done", 2), also
        assert item["last_report_at"] == "2026-01-01T00:00:00Z"


def test_a_sub_boards_relayed_records_are_never_read_twice(_live_crew_log):
    """The parent's log holds copies of the sub-board's records. The tree, which walks
    both boards, and the sub-board's rebuild, which joins slots naming it, read the
    originals only."""
    from kiro_crew.crew_log.projection_work import work_slots_naming_board

    _lead_board()
    _unit(WORKER_UNIT, WORKER)
    _bindings(_live_crew_log, {WORKER: LEAD})
    sub = {"slot": WORKER, "actor": "conductor", "by": WORKER}
    _record(WORKER_UNIT, **sub, action="goal", goal="the sub-goal", round=1, depth=1)
    _record(
        WORKER_UNIT,
        **sub,
        action="create",
        item_id="it_s",
        title="sub task",
        acceptance={"kind": "human_approval"},
    )
    assert [e.data["action"] for e in _lead_entries() if e.data.get("relay")] == [
        "goal",
        "create",
    ]
    assert LEAD not in work_slots_naming_board(WORKER)
    own = crew_log.read_slot_projection(WORKER, "work").value
    joined = crew_log.read_slot_projection(WORKER, "work", also_slots=[LEAD]).value
    assert joined == own
    tree = crew_log.read_tree_projection(LEAD, "worktree").value
    assert tree["dropped"] == 0, tree


def test_a_long_report_history_rebuilds_to_the_boards_own_tail():
    """More reports than any id memory holds: the joined rebuild's tail is the board's
    own, never a tail of duplicated mirrors."""
    _lead_board()
    _unit(WORKER_UNIT, WORKER)
    for n in range(300):
        status = "blocked" if n % 2 else "progress"
        data = {
            **_report(LEAD_UNIT, summary=f"r{n}", status=status),
            "event_id": f"ev-{n}",
            "event": f"r{n}",
            "event_kind": "report",
        }
        emit_write(LEAD_UNIT, "work/recorded", {**data, "board_copy": True})
        emit_write(WORKER_UNIT, "work/recorded", {**data, "mirror": True})
    _settle()
    [own] = crew_log.read_slot_projection(LEAD, "work").value["items"]
    [joined] = crew_log.read_slot_projection(LEAD, "work", also_slots=[WORKER]).value["items"]
    ids = [event["id"] for event in joined["events"]]
    assert ids == [event["id"] for event in own["events"]]
    assert len(ids) == len(set(ids))
    assert joined["summary"] == "r299"


def test_a_relay_into_a_restarted_boards_new_unit_moves_that_unit_last(_live_crew_log):
    """A relayed copy is the first work entry of the board's new unit; the unit order
    must put that unit after its predecessor, as a local record would."""
    from kiro_crew import session_ledger

    _lead_board()
    _unit(WORKER_UNIT, WORKER)
    _bindings(_live_crew_log, {WORKER: LEAD})
    _unit("u-lead-2", LEAD)
    sub = {"slot": WORKER, "actor": "conductor", "by": WORKER}
    _record(WORKER_UNIT, **sub, action="goal", goal="the sub-goal", round=1, depth=1)
    assert [e.data.get("action") for e in _lead_entries("u-lead-2")] == ["goal"]
    assert session_ledger.work_crew_log_units(LEAD)[-1] == "u-lead-2"


def test_the_rebuild_never_stores_the_folds_waiting_text_as_a_summary():
    from kiro_crew import work_ledger as wl

    waiting = {"item_id": "it_a", "status": None, "summary": crew_log.WORK_AWAITING_REPORT}
    assert wl._stored_view(waiting)["summary"] == ""
    reported = {"item_id": "it_a", "status": "progress", "summary": "half way"}
    assert wl._stored_view(reported) == reported


def test_a_binding_cycle_never_folds_a_boards_own_record_twice(_live_crew_log):
    """A under B and B under A: A's record goes to B and stops there."""
    other = "chat-own-units-other"
    _bindings(_live_crew_log, {LEAD: other, other: LEAD})
    _unit("u-other", other)
    _record("u-other", slot=other, actor="conductor", by=other, action="goal", goal="o", round=1)
    _lead_board(bind=None)
    lead_records = [
        e for e in _lead_entries() if e.type == "work/recorded" and e.data.get("slot") == LEAD
    ]
    assert [e.data["action"] for e in lead_records] == ["goal", "create"]
    assert not any(e.data.get("relay") for e in lead_records), "a copy came home"
    relayed = [e for e in _lead_entries("u-other") if e.data.get("slot") == LEAD]
    assert [e.data["action"] for e in relayed] == ["goal", "create"]


def _size(unit: str) -> int:
    from kiro_crew.crew_log.store_paths import _store_name, crew_log_root

    store = crew_log_root(lg.KIND_SESSION) / _store_name(unit)
    return sum(f.stat().st_size for f in store.rglob("*") if f.is_file())


def test_after_a_restart_reports_and_relays_land_in_the_new_unit(_live_crew_log):
    """A conductor restart opens a unit with no work entry yet; that unit takes the
    next report and the next relayed charge, and the old unit does not grow."""
    from types import SimpleNamespace

    from kiro_crew.dashboard.handlers.work_ledger_recording import _conductor_unit

    _lead_board()
    _unit(WORKER_UNIT, WORKER)
    _bindings(_live_crew_log, {WORKER: LEAD})
    _turn(WORKER_UNIT, 1, 1.0)
    assert [e.data["credits"] for e in _lead_entries() if e.type == "work/charged"] == [1.0]
    _unit("u-lead-2", LEAD)
    before = _size(LEAD_UNIT)

    target = _conductor_unit(SimpleNamespace(sessions=None), LEAD)
    assert target == "u-lead-2"
    _record(target, **_report(target, summary="after restart"))
    _turn(WORKER_UNIT, 2, 2.0)

    assert _size(LEAD_UNIT) == before, "the retired unit grew"
    new = _lead_entries("u-lead-2")
    assert [e.data["credits"] for e in new if e.type == "work/charged"] == [2.0]
    assert [e.data.get("summary") for e in new if e.type == "work/recorded"] == ["after restart"]
    item = _item()
    assert (item["status"], item["summary"]) == ("progress", "after restart")
    assert _state("workstreams")["spend"][WORKER]["credits"] == 3.0


def test_a_unit_whose_header_lands_after_a_lookup_becomes_the_newest(tmp_path):
    """A create makes the directory first and publishes the header second. A lookup in
    between must not pin the older unit once the header lands inside the directory."""
    from kiro_crew import session_ledger
    from kiro_crew.crew_log.store_paths import _store_name, crew_log_root

    _lead_board()
    _unit("u-lead-2", LEAD)
    store = crew_log_root(lg.KIND_SESSION) / _store_name("u-lead-2")
    parked = tmp_path / "parked"
    parked.mkdir()
    segments = [p for p in store.iterdir() if p.is_file()]
    for segment in segments:  # the directory exists, its header does not yet
        segment.rename(parked / segment.name)
    assert session_ledger.newest_work_unit(LEAD) == LEAD_UNIT
    for segment in segments:
        (parked / segment.name).rename(segment)
    assert session_ledger.newest_work_unit(LEAD) == "u-lead-2"


def test_a_lookup_before_the_replacements_opener_lands_is_not_kept(_live_crew_log):
    """A replacement whose header is older on the clock (a backward step) and whose
    ``session/opened`` has not landed yet sorts first; once the opener names its
    predecessor, the next lookup returns the replacement."""
    from kiro_crew import session_ledger
    from kiro_crew.crew_log import store

    _lead_board()
    with pytest.MonkeyPatch.context() as clock:
        clock.setattr(store, "now_ms", lambda: 1_000)
        replacement = _unit("u-lead-2", LEAD)
    assert session_ledger.newest_work_unit(LEAD) == LEAD_UNIT
    replacement.append(
        "session/opened",
        {
            "agent": "lead",
            "slot": LEAD,
            "model": "opus",
            "cwd": "/",
            "owner": "owner",
            "resumed": False,
            "previous": {"sid": LEAD_UNIT},
        },
        src="gateway",
    )
    assert session_ledger.newest_work_unit(LEAD) == "u-lead-2"


def test_a_charge_in_a_binding_cycle_is_billed_once(_live_crew_log):
    """A under B and B under A: A's charge goes to B and is not sent back to A."""
    other = "chat-own-units-other"
    _unit("u-other", other)
    _record("u-other", slot=other, actor="conductor", by=other, action="goal", goal="o", round=1)
    _lead_board(bind=None)
    _bindings(_live_crew_log, {LEAD: other, other: LEAD})
    _turn(LEAD_UNIT, 1, 3.0)
    assert not [e for e in _lead_entries() if e.type == "work/charged"], "a charge came home"
    assert [e.data["by"] for e in _lead_entries("u-other") if e.type == "work/charged"] == [LEAD]
    own = _state("workstreams")["spend"].get(LEAD)
    assert own is None or own["credits"] <= 3.0, own


def test_a_changed_binding_is_read_on_the_next_closer(_live_crew_log):
    """The binding is cached against its file; rewriting the file is seen."""
    from kiro_crew import work_ledger

    path = work_ledger.binding_path(WORKER)
    path.parent.mkdir(parents=True, exist_ok=True)
    assert work_relay._bound_board(WORKER) == ""
    path.write_text('{"conductor_slot_key": "board-a", "item_id": "it_0000000a"}')
    assert work_relay._bound_board(WORKER) == "board-a"
    calls = []
    real = work_ledger.read_binding
    _live_crew_log.setattr(
        work_ledger, "read_binding", lambda w, **kw: calls.append(w) or real(w, **kw)
    )
    assert work_relay._bound_board(WORKER) == "board-a"
    assert calls == [], "an unchanged binding file was read again"
    path.write_text('{"conductor_slot_key": "board-bb", "item_id": "it_0000000b"}')
    assert work_relay._bound_board(WORKER) == "board-bb"
    path.unlink()
    assert work_relay._bound_board(WORKER) == ""


def test_a_binding_read_that_fails_is_retried_on_the_next_closer(_live_crew_log):
    """A read error is not cached as "unbound": once reads recover the charge relays."""
    from kiro_crew import work_ledger

    _lead_board()
    _unit(WORKER_UNIT, WORKER)
    path = work_ledger.binding_path(WORKER)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'{{"conductor_slot_key": "{LEAD}", "item_id": "it_0000000a"}}')
    real = work_ledger.read_binding
    failures = [OSError("read failed")]

    def flaky(worker: str, *, strict: bool = False):
        if failures:
            raise failures.pop()
        return real(worker, strict=strict)

    _live_crew_log.setattr(work_ledger, "read_binding", flaky)
    assert work_relay._bound_board(WORKER) == ""
    assert WORKER not in work_relay._BINDINGS, "a failed read was cached"
    _turn(WORKER_UNIT, 1, 2.0)
    charges = [e for e in _lead_entries() if e.type == "work/charged"]
    assert [c.data["credits"] for c in charges] == [2.0]


def test_a_relay_with_no_board_unit_yet_is_sent_when_one_exists(_live_crew_log):
    """A charge that found no unit for its board is not remembered as sent."""
    _unit(WORKER_UNIT, WORKER)
    _bindings(_live_crew_log, {WORKER: LEAD})
    _turn(WORKER_UNIT, 1, 1.0)  # the lead has recorded nothing: no unit to relay into
    _lead_board(bind=WORKER)
    _turn(WORKER_UNIT, 2, 2.0)
    charges = [e for e in _lead_entries() if e.type == "work/charged"]
    assert [c.data["credits"] for c in charges] == [2.0]
