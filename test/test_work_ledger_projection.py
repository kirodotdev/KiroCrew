"""The work ledger as a projection of the crew log.

Every write the two routes accept is first gated on the crew log and then
recorded as one ``work/recorded`` entry in the acting session's log; the JSON
under ``work-ledger/<conductor>/`` is a cache that ``rebuild_from_projection``
re-materialises from the ``work`` fold. These tests pin the gate (refuse when the
emitter is off or the caller's unit is unknown, writing nothing), the entry per
mutation carrying only the fields that action set, and the rebuild round trip.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
import work_ledger_projection_helpers
from work_ledger_projection_helpers import (
    _SLOTS,
    ACCEPTANCE,
    CONDUCTOR,
    WORKER,
    _board,
    _capture_probes,
    _real_units,
    _rebuild,
    _record,
    _rendered,
    _report,
    _req,
    _Slot,
    _width,
)

from kiro_crew import work_ledger as wl
from kiro_crew.crew_log import projection
from kiro_crew.dashboard.handlers import work_ledger as routes
from kiro_crew.testing.wait import until_parked

# The shared fixtures, bound here by name so pytest registers them for this module.
_isolated = work_ledger_projection_helpers._isolated
recorded = work_ledger_projection_helpers.recorded


# -- the gate ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_writes_are_refused_when_the_crew_log_is_off(recorded, monkeypatch):
    item_id = await _board()
    monkeypatch.setattr(routes.crew_log_emit, "enabled", lambda: False)
    before = len(recorded)

    status, body = await _report(WORKER, {"status": "progress", "summary": "half"})
    assert (status, body["code"]) == (409, "crew_log_off"), body
    status, body = await _record(
        CONDUCTOR, {"action": "decide", "item_id": item_id, "decision": "go"}
    )
    assert (status, body["code"]) == (409, "crew_log_off"), body

    item = wl.read_work_item(CONDUCTOR, item_id)
    assert item is not None and item.status is None and item.decision == ""
    assert len(recorded) == before


@pytest.mark.asyncio
async def test_writes_are_refused_when_the_callers_unit_is_unknown(recorded, monkeypatch):
    item_id = await _board()
    monkeypatch.setattr(routes, "unit_for_session_key", lambda sessions, key: routes.UNKNOWN)
    before = len(recorded)

    status, body = await _report(WORKER, {"status": "done", "summary": "finished"})
    assert (status, body["code"]) == (409, "crew_log_unit_unknown"), body
    item = wl.read_work_item(CONDUCTOR, item_id)
    assert item is not None and item.status is None
    assert len(recorded) == before


# -- one entry per mutation ----------------------------------------------------


@pytest.mark.asyncio
async def test_each_write_records_one_entry_with_the_fields_it_set(recorded):
    item_id = await _board()
    status, body = await _report(WORKER, {"status": "progress", "summary": "half way", "pr": 12})
    assert status == 200, body

    assert [data["action"] for _unit, data in recorded] == ["goal", "create", "bind", "report"]
    assert [unit for unit, _data in recorded] == [f"unit:{CONDUCTOR}"] * 3 + [f"unit:{WORKER}"]
    goal, create, bind, report = (data for _unit, data in recorded)

    assert (goal["slot"], goal["by"], goal["actor"]) == (CONDUCTOR, CONDUCTOR, "conductor")
    assert (goal["goal"], goal["round"], goal["depth"]) == ("ship it", 1, 0)
    assert "item_id" not in goal and "event_kind" not in goal

    assert (create["item_id"], create["title"]) == (item_id, "item one")
    assert (create["acceptance"], create["event_kind"]) == (ACCEPTANCE, "create")
    assert "state" not in create and "worker_session_key" not in create

    assert (bind["item_id"], bind["worker_session_key"]) == (item_id, WORKER)
    assert bind["event_kind"] == "bind"

    assert (report["slot"], report["by"], report["actor"]) == (CONDUCTOR, WORKER, "worker")
    assert (report["item_id"], report["status"], report["pr"]) == (item_id, "progress", 12)
    assert (report["summary"], report["event"], report["event_kind"]) == (
        "half way",
        "half way",
        "report",
    )
    assert "verdict" not in report and "decision" not in report

    for _unit, data in recorded:
        assert None not in data.values()


@pytest.mark.asyncio
async def test_caller_text_is_redacted_before_both_cache_and_log_are_written(recorded):
    """Both route entry points cross the same boundary before their fit probes
    and commits, so secrets and suspicious URLs reach neither durable copy."""
    secret = "AKIAIOSFODNN7EXAMPLE"
    url = "https://evil.example.com/steal?data=" + "A" * 250
    acceptance = {"kind": "human_approval", "notes": [secret, url]}

    status, body = await _record(
        CONDUCTOR, {"action": "goal", "goal": f"ship with {secret}", "round": 1}
    )
    assert status == 200, body
    status, body = await _record(
        CONDUCTOR,
        {"action": "create", "title": f"remove {secret}", "acceptance": acceptance},
    )
    assert status == 200, body
    item_id = body["item"]["item_id"]
    _SLOTS[WORKER] = _Slot(created_by=CONDUCTOR)
    status, body = await _record(
        CONDUCTOR, {"action": "bind", "item_id": item_id, "worker_session_key": WORKER}
    )
    assert status == 200, body
    status, body = await _report(
        WORKER,
        {
            "status": "progress",
            "summary": f"found {secret}",
            "artifacts": {"diagnostic": url},
        },
    )
    assert status == 200, body
    status, body = await _record(
        CONDUCTOR,
        {"action": "decide", "item_id": item_id, "decision": f"do not fetch {url}"},
    )
    assert status == 200, body

    header = wl.read_conductor(CONDUCTOR)
    item = wl.read_work_item(CONDUCTOR, item_id)
    assert header is not None and item is not None
    entries = {data["action"]: data for _unit, data in recorded if data["action"] != "bind"}
    assert entries["goal"]["goal"] == header.goal
    assert entries["create"]["title"] == item.title
    assert entries["create"]["acceptance"] == item.acceptance
    assert entries["report"]["summary"] == item.summary
    assert entries["report"]["artifacts"] == item.artifacts
    assert entries["decide"]["decision"] == item.decision
    assert entries["create"]["event"] == item.title
    assert entries["report"]["event"] == item.summary
    assert entries["decide"]["event"] == item.decision

    durable = json.dumps(
        {"header": header.to_dict(), "item": item.to_dict(), "entries": recorded},
        ensure_ascii=False,
    )
    assert secret not in durable
    assert url not in durable
    assert "[REDACTED:" in durable


@pytest.mark.asyncio
async def test_credential_in_artifact_key_is_redacted_before_storage(recorded):
    secret = "AKIAIOSFODNN7EXAMPLE"
    item_id = await _board()

    status, body = await _report(
        WORKER,
        {
            "status": "progress",
            "summary": "credential found",
            "artifacts": {f"diagnostic-{secret}": "captured evidence"},
        },
    )

    assert status == 200, body
    item = wl.read_work_item(CONDUCTOR, item_id)
    assert item is not None
    [stored_key] = item.artifacts
    assert secret not in stored_key
    assert "[REDACTED:" in stored_key
    assert item.artifacts[stored_key] == "captured evidence"


@pytest.mark.asyncio
async def test_cache_and_crew_log_keep_byte_identical_redacted_fields(recorded):
    secret = "AKIAIOSFODNN7EXAMPLE"
    item_id = await _board()

    status, body = await _report(
        WORKER,
        {
            "status": "progress",
            "summary": f"found {secret}",
            "artifacts": {f"diagnostic-{secret}": f"captured-{secret}"},
        },
    )

    assert status == 200, body
    item = wl.read_work_item(CONDUCTOR, item_id)
    assert item is not None
    cached_event = json.loads(
        wl.item_events_path(CONDUCTOR, item_id).read_text(encoding="utf-8").splitlines()[-1]
    )
    entry = recorded[-1][1]
    cached = json.dumps(
        {"summary": item.summary, "artifacts": item.artifacts, "event": cached_event["text"]},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    logged = json.dumps(
        {"summary": entry["summary"], "artifacts": entry["artifacts"], "event": entry["event"]},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    assert cached == logged
    assert secret.encode() not in cached
    assert b"[REDACTED:" in cached


@pytest.mark.asyncio
async def test_two_artifact_keys_redacted_to_empty_are_refused_without_writing(
    recorded, monkeypatch
):
    item_id = await _board()
    before = len(recorded)

    def _redaction_failure(_text: str) -> tuple[str, int]:
        raise RuntimeError("redaction unavailable")

    monkeypatch.setattr(routes.crew_log_emit, "redact_exfiltration_urls", _redaction_failure)
    status, body = await _report(
        WORKER,
        {
            "status": "progress",
            "summary": "still working",
            "artifacts": {"first pointer": "one", "second pointer": "two"},
        },
    )

    assert (status, body.get("code")) == (400, wl.CODE_INVALID_VALUE), body
    item = wl.read_work_item(CONDUCTOR, item_id)
    assert item is not None
    assert (item.status, item.summary, item.artifacts) == (None, "", {})
    assert len(recorded) == before


@pytest.mark.asyncio
async def test_non_colliding_nested_acceptance_round_trips_through_cache_and_log(recorded):
    acceptance = {
        "kind": "human_approval",
        "requirements": {
            "primary": {"label": "review"},
            "fallback": ["owner"],
        },
    }

    status, body = await _record(
        CONDUCTOR,
        {"action": "create", "title": "nested acceptance", "acceptance": acceptance},
    )

    assert status == 200, body
    item = wl.read_work_item(CONDUCTOR, body["item"]["item_id"])
    assert item is not None and item.acceptance == acceptance
    assert recorded[-1][1]["acceptance"] == acceptance


# -- the cache is rebuilt from the fold ---------------------------------------


def test_rebuild_materialises_header_items_and_events_from_the_fold(monkeypatch):
    item_id = "it_0000abcd"
    monkeypatch.setattr(
        projection,
        "read_slot_projection",
        lambda slot, name, **_kw: SimpleNamespace(
            value=_rendered(slot, item_id) if name == "work" else {}
        ),
    )
    counts = wl.rebuild_from_projection(CONDUCTOR)
    assert counts == {"slot_key": CONDUCTOR, "items": 1, "events": 2, "removed": 0, "legacy": 0}

    header = wl.read_conductor(CONDUCTOR)
    assert header is not None and (header.goal, header.round) == ("ship it", 2)
    item = wl.read_work_item(CONDUCTOR, item_id)
    assert item is not None
    assert (item.title, item.status, item.pr, item.worker_session_key) == (
        "item one",
        "progress",
        12,
        WORKER,
    )
    lines = wl.item_events_path(CONDUCTOR, item_id).read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["kind"] for line in lines] == ["create", "report"]


def test_rebuild_of_an_empty_fold_writes_nothing(monkeypatch):
    monkeypatch.setattr(
        projection,
        "read_slot_projection",
        lambda slot, name, **_kw: SimpleNamespace(
            value={"conductor": {}, "items": [], "omitted": 0}
        ),
    )
    counts = wl.rebuild_from_projection(CONDUCTOR)
    assert counts == {"slot_key": CONDUCTOR, "items": 0, "events": 0, "removed": 0}
    assert wl.read_conductor(CONDUCTOR) is None


def test_a_fold_at_its_item_ceiling_refuses_the_rebuild_even_on_a_dirty_cache(monkeypatch):
    """The fold keeps a board's first ``WORK_ITEM_LIMIT`` items; a full fold may
    be a prefix of a board whose closed items were archived out of ``items/``.
    Rebuilding from it would re-materialise the prefix and, past the dirty-cache
    shortcut, unlink every newer item. So it refuses -- and the cache stands."""
    monkeypatch.setattr(projection, "WORK_ITEM_LIMIT", 1)
    fold = _rendered(CONDUCTOR, "it_0000abcd")  # exactly one item: the ceiling
    fold["omitted"] = 1  # ... and a later create the fold could not hold
    monkeypatch.setattr(
        projection,
        "read_slot_projection",
        lambda slot, name, **_kw: SimpleNamespace(value=fold if name == "work" else {}),
    )
    # A live board holding a NEWER item the fold never saw, flagged dirty (the one
    # state whose documented cure is this rebuild).
    wl.ensure_conductor(CONDUCTOR, goal="live")
    newer = wl.apply_conductor_action(CONDUCTOR, "create", title="newest", acceptance={})["item"]
    wl.mark_cache_dirty(CONDUCTOR, "an unrecorded write could not be undone")
    before = {p.name: p.read_bytes() for p in wl.items_dir(CONDUCTOR).iterdir()}

    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.rebuild_from_projection(CONDUCTOR)

    assert caught.value.code == wl.CODE_CREW_LOG_INCOMPLETE
    assert "ceiling" in str(caught.value)
    assert {p.name: p.read_bytes() for p in wl.items_dir(CONDUCTOR).iterdir()} == before
    assert wl.read_work_item(CONDUCTOR, newer.item_id) is not None
    assert wl.read_work_item(CONDUCTOR, "it_0000abcd") is None
    assert wl.cache_dirty(CONDUCTOR), "the refusal leaves the flag for the operator"


def test_a_full_fold_that_omitted_nothing_is_the_whole_board_and_rebuilds(monkeypatch):
    """Exactly ``WORK_ITEM_LIMIT`` items and ``omitted == 0`` is a complete record,
    not a prefix: the rebuild proceeds."""
    monkeypatch.setattr(projection, "WORK_ITEM_LIMIT", 1)
    fold = _rendered(CONDUCTOR, "it_0000abcd")  # exactly one item, nothing omitted
    monkeypatch.setattr(
        projection,
        "read_slot_projection",
        lambda slot, name, **_kw: SimpleNamespace(value=fold if name == "work" else {}),
    )
    result = wl.rebuild_from_projection(CONDUCTOR)
    assert result["items"] == 1
    assert wl.read_work_item(CONDUCTOR, "it_0000abcd") is not None


def test_a_rebuild_sets_the_create_counter_to_what_the_record_holds(monkeypatch):
    """The create counter is rebuilt from the fold, with a trusted header as a floor.

    A cache header can hold a count the record does not: higher, after a create
    whose entry never landed and whose record was then removed, or after the
    one-time seed from an old board's files; lower, after a hand edit. Rebuilt,
    ``created_total`` is the number of items the fold retained -- past the ceiling
    guard the fold dropped no create, and its ``omitted`` there counts entries that
    are not this board's creates -- raised to a trusted header's count, since the
    record's pieces can come out short and the spend count must not drop below what
    was spent. With the fold's one item a counter of 7 stays 7, and one of 0 comes
    up to 1.
    """
    fold = _rendered(CONDUCTOR, "it_0000abcd")
    fold["omitted"] = 3  # stragglers of a purged board: not creates, not counted
    monkeypatch.setattr(
        projection,
        "read_slot_projection",
        lambda slot, name, **_kw: SimpleNamespace(value=fold if name == "work" else {}),
    )
    wl.ensure_conductor(CONDUCTOR, goal="ship it")
    header_path = wl.conductor_dir(CONDUCTOR) / "conductor.json"
    for stale in (7, 0):
        stored = json.loads(header_path.read_text(encoding="utf-8"))
        stored["created_total"] = stale
        header_path.write_text(json.dumps(stored), encoding="utf-8")
        assert wl.read_conductor(CONDUCTOR).created_total == stale

        counts = wl.rebuild_from_projection(CONDUCTOR)

        assert (counts["items"], counts["legacy"]) == (1, 0)
        header = wl.read_conductor(CONDUCTOR)
        # A trusted header's count is a floor: 7 stays 7, 0 comes up to 1.
        assert header is not None and header.created_total == max(stale, 1)
    # The rebuilt counter is live: the next create lands and bumps it.
    wl.apply_conductor_action(CONDUCTOR, "create", title="second", acceptance={})
    assert wl.read_conductor(CONDUCTOR).created_total == 2


def _seed_cached_cap(goal_version: int, item_cap: int, base: int, concurrency_cap: int = 6) -> None:
    """Write a cached header carrying the goal's caps, as goal writes would leave it."""
    wl.ensure_conductor(CONDUCTOR, goal="ship it")
    header_path = wl.conductor_dir(CONDUCTOR) / "conductor.json"
    stored = json.loads(header_path.read_text(encoding="utf-8"))
    stored.update(
        goal_version=goal_version,
        item_cap=item_cap,
        concurrency_cap=concurrency_cap,
        goal_items_base=base,
    )
    header_path.write_text(json.dumps(stored), encoding="utf-8")


def test_a_clean_rebuild_keeps_the_cached_goal_caps(monkeypatch):
    """The caps ride no log entry, so a cache in step with the record keeps them."""
    fold = _rendered(CONDUCTOR, "it_0000abcd")
    fold["conductor"]["goal_version"] = 1
    monkeypatch.setattr(
        projection,
        "read_slot_projection",
        lambda slot, name, **_kw: SimpleNamespace(value=fold if name == "work" else {}),
    )
    _seed_cached_cap(goal_version=1, item_cap=50, base=1, concurrency_cap=9)
    wl.rebuild_from_projection(CONDUCTOR)
    header = wl.read_conductor(CONDUCTOR)
    assert (header.item_cap, header.concurrency_cap, header.goal_items_base) == (50, 9, 1)


def test_a_clean_rebuild_of_an_upgraded_header_counts_from_the_upgrade(monkeypatch):
    """A trusted header with no stored count start keeps its legacy status."""
    fold = _rendered(CONDUCTOR, "it_0000abcd")
    fold["conductor"]["goal_version"] = 1
    monkeypatch.setattr(
        projection,
        "read_slot_projection",
        lambda slot, name, **_kw: SimpleNamespace(value=fold if name == "work" else {}),
    )
    _seed_cached_cap(goal_version=1, item_cap=20, base=0)
    header_path = wl.conductor_dir(CONDUCTOR) / "conductor.json"
    stored = json.loads(header_path.read_text(encoding="utf-8"))
    stored.pop("goal_items_base")
    header_path.write_text(json.dumps(stored), encoding="utf-8")
    wl.rebuild_from_projection(CONDUCTOR)
    header = wl.read_conductor(CONDUCTOR)
    assert (header.created_total, header.goal_items_base, header.goal_items_used) == (1, 1, 0)


def test_a_dirty_rebuild_drops_a_refused_goal_writes_caps_and_count_start(monkeypatch):
    """A goal write the record never got can carry raised caps and a reset count.

    The rebuild puts the recorded goal back, so it must not keep that write's bounds:
    it falls back to the default caps, counted from the board's first create.
    """
    fold = _rendered(CONDUCTOR, "it_0000abcd")
    fold["conductor"]["goal_version"] = 1
    monkeypatch.setattr(
        projection,
        "read_slot_projection",
        lambda slot, name, **_kw: SimpleNamespace(value=fold if name == "work" else {}),
    )
    _seed_cached_cap(goal_version=2, item_cap=200, base=1, concurrency_cap=30)
    wl.mark_cache_dirty(CONDUCTOR, "an unrecorded write could not be undone")
    wl.rebuild_from_projection(CONDUCTOR)
    header = wl.read_conductor(CONDUCTOR)
    assert header.goal == "ship it"
    assert (header.item_cap, header.concurrency_cap, header.goal_items_base) == (
        wl.DEFAULT_GOAL_ITEM_CAP,
        wl.DEFAULT_GOAL_CONCURRENCY_CAP,
        0,
    )
    assert header.goal_items_used == header.created_total == 1


def test_a_bound_workers_real_unit_joins_the_fold_and_its_report_is_rebuilt():
    """The report lives in the WORKER's log, whose header names the worker's own
    slot; the fold reaches it through the conductor's ``bind`` entry, and another
    board's entry in that same worker log stays out."""
    from kiro_crew import crew_log as lg
    from kiro_crew.crew_log import CrewLog

    item_id = "it_0000abcd"
    conductor = CrewLog.create(
        lg.KIND_SESSION, "u-conductor", owner="raymond", agent="kirocrew", slot=CONDUCTOR
    )
    worker = CrewLog.create(
        lg.KIND_SESSION, "u-worker", owner="raymond", agent="kirocrew", slot=WORKER
    )
    mine = {"slot": CONDUCTOR, "actor": "conductor", "by": CONDUCTOR}
    conductor.append(
        "work/recorded",
        {**mine, "action": "goal", "goal": "ship it", "round": 1, "depth": 0},
        src="gateway",
    )
    conductor.append(
        "work/recorded",
        {
            **mine,
            "action": "create",
            "item_id": item_id,
            "title": "item one",
            "acceptance": ACCEPTANCE,
            "event": "item one",
            "event_kind": "create",
        },
        src="gateway",
    )
    conductor.append(
        "work/recorded",
        {
            **mine,
            "action": "bind",
            "item_id": item_id,
            "worker_session_key": WORKER,
            "event": "bound",
            "event_kind": "bind",
        },
        src="gateway",
    )
    worker.append(
        "work/recorded",
        {
            "slot": CONDUCTOR,
            "actor": "worker",
            "by": WORKER,
            "action": "report",
            "item_id": item_id,
            "status": "progress",
            "summary": "half way",
            "pr": 12,
            "event": "half way",
            "event_kind": "report",
        },
        src="gateway",
    )
    # The same worker reporting to ANOTHER conductor's board: not this fold's.
    worker.append(
        "work/recorded",
        {
            "slot": "chat-other-conductor",
            "actor": "worker",
            "by": WORKER,
            "action": "report",
            "item_id": "it_0000beef",
            "status": "done",
            "summary": "elsewhere",
            "event": "elsewhere",
            "event_kind": "report",
        },
        src="gateway",
    )

    folded = projection.read_slot_projection(CONDUCTOR, "work").value
    [item] = folded["items"]
    assert (item["item_id"], item["status"], item["summary"], item["pr"]) == (
        item_id,
        "progress",
        "half way",
        12,
    )
    assert [event["kind"] for event in item["events"]] == ["create", "bind", "report"]

    counts = wl.rebuild_from_projection(CONDUCTOR)
    assert counts == {"slot_key": CONDUCTOR, "items": 1, "events": 3, "removed": 0, "legacy": 0}
    rebuilt = wl.read_work_item(CONDUCTOR, item_id)
    assert rebuilt is not None and (rebuilt.status, rebuilt.summary, rebuilt.pr) == (
        "progress",
        "half way",
        12,
    )
    assert wl.read_work_item(CONDUCTOR, "it_0000beef") is None
    lines = wl.item_events_path(CONDUCTOR, item_id).read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["kind"] for line in lines] == ["create", "bind", "report"]


def test_work_fold_uses_append_order_when_header_clocks_go_backward(monkeypatch):
    """A later work append wins even when its unit header carries an older clock."""
    from kiro_crew import crew_log as lg
    from kiro_crew import session_ledger
    from kiro_crew.crew_log import CrewLog, emit, store

    older_unit = "u-work-clock-older"
    newer_unit = "u-work-clock-newer"
    monkeypatch.setattr(emit, "enabled", lambda: True)

    monkeypatch.setattr(store, "now_ms", lambda: 2_000)
    first = CrewLog.create(
        lg.KIND_SESSION, older_unit, owner="raymond", agent="kirocrew", slot=CONDUCTOR
    )
    del first
    assert emit.on_work_recorded(
        older_unit,
        {
            "slot": CONDUCTOR,
            "actor": "conductor",
            "by": CONDUCTOR,
            "action": "goal",
            "goal": "actually older",
            "round": 1,
            "depth": 0,
        },
    )

    # The second unit is created and appended later, but an NTP correction gives
    # its header the earlier wall clock and makes the raw store order backwards.
    monkeypatch.setattr(store, "now_ms", lambda: 1_000)
    second = CrewLog.create(
        lg.KIND_SESSION, newer_unit, owner="raymond", agent="kirocrew", slot=CONDUCTOR
    )
    del second
    assert emit.on_work_recorded(
        newer_unit,
        {
            "slot": CONDUCTOR,
            "actor": "conductor",
            "by": CONDUCTOR,
            "action": "goal",
            "goal": "actually newer",
            "round": 2,
            "depth": 0,
        },
    )

    # A later ledger-only append in the retired unit must not reorder work entries.
    session_ledger.record(CONDUCTOR, session_id=older_unit, goal="unrelated ledger update")

    assert store.session_units_for_slot(CONDUCTOR) == (newer_unit, older_unit)
    assert session_ledger.work_crew_log_units(CONDUCTOR) == (older_unit, newer_unit)
    board = projection.read_slot_projection(CONDUCTOR, "work").value
    assert (board["conductor"]["goal"], board["conductor"]["round"]) == (
        "actually newer",
        2,
    )


def test_an_out_of_range_entry_time_does_not_escape_the_work_fold():
    from kiro_crew import crew_log as lg
    from kiro_crew.crew_log import CrewLog, store

    item_id = "it_0000abcd"
    unit_id = "u-out-of-range-time"
    unit = CrewLog.create(
        lg.KIND_SESSION, unit_id, owner="raymond", agent="kirocrew", slot=CONDUCTOR
    )
    unit.append(
        "work/recorded",
        {
            "slot": CONDUCTOR,
            "actor": "conductor",
            "by": CONDUCTOR,
            "action": "create",
            "item_id": item_id,
            "title": "survives",
            "event": "survives",
            "event_kind": "create",
        },
        src="gateway",
    )
    del unit

    path = store.crew_log_path(lg.KIND_SESSION, unit_id)
    lines = path.read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[-1])
    entry["time"] = 10**19
    lines[-1] = json.dumps(entry)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    board = projection.read_slot_projection(CONDUCTOR, "work").value
    [item] = board["items"]
    assert item["title"] == "survives"
    assert board["conductor"]["first_entry_at"] == ""
    assert board["conductor"]["created_at"] == ""
    assert item["created_at"] == ""
    assert item["events"][0]["ts"] == ""


# -- the shipped caller of the rebuild ----------------------------------------


@pytest.mark.asyncio
async def test_the_rebuild_route_refuses_when_the_crew_log_is_off(monkeypatch):
    monkeypatch.setattr(routes.crew_log_emit, "enabled", lambda: False)
    status, body = await _rebuild(CONDUCTOR)
    assert (status, body["code"]) == (409, "crew_log_off"), body
    assert wl.read_conductor(CONDUCTOR) is None


@pytest.mark.asyncio
async def test_the_rebuild_route_rebuilds_the_callers_own_board_from_real_units(monkeypatch):
    from kiro_crew import crew_log as lg
    from kiro_crew.crew_log import CrewLog

    monkeypatch.setattr(routes.crew_log_emit, "enabled", lambda: True)
    item_id = "it_0000abcd"
    conductor = CrewLog.create(
        lg.KIND_SESSION, "u-conductor", owner="raymond", agent="kirocrew", slot=CONDUCTOR
    )
    mine = {"slot": CONDUCTOR, "actor": "conductor", "by": CONDUCTOR}
    conductor.append(
        "work/recorded",
        {**mine, "action": "goal", "goal": "ship it", "round": 1, "depth": 0},
        src="gateway",
    )
    conductor.append(
        "work/recorded",
        {
            **mine,
            "action": "create",
            "item_id": item_id,
            "title": "item one",
            "acceptance": ACCEPTANCE,
            "event": "item one",
            "event_kind": "create",
        },
        src="gateway",
    )
    assert wl.read_conductor(CONDUCTOR) is None

    status, body = await _rebuild(CONDUCTOR)
    assert (status, body) == (
        200,
        {"ok": True, "slot_key": CONDUCTOR, "items": 1, "events": 1, "removed": 0, "legacy": 0},
    )
    header = wl.read_conductor(CONDUCTOR)
    assert header is not None and header.goal == "ship it"
    item = wl.read_work_item(CONDUCTOR, item_id)
    assert item is not None and item.title == "item one"


# -- the record is refused before the commit, and answered for after it -------


@pytest.mark.asyncio
async def test_a_mutation_whose_record_would_not_fit_a_log_line_is_refused_whole(recorded):
    """The store would take a 70 KB acceptance (its cap is 500 KB); the crew log's
    line cap would not. The refusal comes BEFORE the store writes: no item file,
    no entry, and the caller learns which bound it hit."""
    status, body = await _record(CONDUCTOR, {"action": "goal", "goal": "ship it", "round": 1})
    assert status == 200, body
    before = len(recorded)

    status, body = await _record(
        CONDUCTOR,
        {"action": "create", "title": "too big", "acceptance": {"text": "a" * 70_000}},
    )
    assert (status, body["code"]) == (400, "work_entry_too_large"), body
    assert wl.list_work_items(CONDUCTOR) == []
    assert len(recorded) == before


@pytest.mark.asyncio
async def test_the_entry_carries_the_committed_values_not_the_request(recorded):
    """An omitted `artifacts` CLEARS the map at commit and an omitted `pr` keeps the
    earlier one; the entry says exactly that, so the fold rebuilds the cache."""
    item_id = await _board()
    status, body = await _report(
        WORKER,
        {"status": "progress", "summary": "first", "artifacts": {"repo": "a/b"}, "pr": 7},
    )
    assert status == 200, body
    status, body = await _report(WORKER, {"status": "progress", "summary": "second"})
    assert status == 200, body

    first, second = (data for _unit, data in recorded[-2:])
    assert (first["artifacts"], first["pr"]) == ({"repo": "a/b"}, 7)
    assert (second["artifacts"], second["pr"], second["summary"]) == ({}, 7, "second")
    item = wl.read_work_item(CONDUCTOR, item_id)
    assert item is not None and (item.artifacts, item.pr) == ({}, 7)


@pytest.mark.asyncio
async def test_an_unconfirmed_append_is_answered_as_a_failure_naming_the_committed_item(
    recorded, monkeypatch
):
    item_id = await _board()
    monkeypatch.setattr(routes.crew_log_emit, "on_work_recorded", lambda unit, data: False)

    status, body = await _report(WORKER, {"status": "progress", "summary": "half"})
    assert (status, body["code"], body["item_id"]) == (503, "crew_log_unrecorded", item_id), body
    status, body = await _record(
        CONDUCTOR, {"action": "create", "title": "item two", "acceptance": ACCEPTANCE}
    )
    assert (status, body["code"]) == (503, "crew_log_unrecorded"), body
    # The create was undone with the refusal: the id is returned, the item is gone.
    assert body["item_id"] != item_id and wl.read_work_item(CONDUCTOR, body["item_id"]) is None


def test_on_work_recorded_acknowledges_only_an_append_that_landed(monkeypatch):
    """Against the real writer: a valid entry returns True and is in the log; one
    the type refuses returns False; with the emitter off nothing is promised."""
    from kiro_crew import crew_log as lg
    from kiro_crew.crew_log import CrewLog, emit

    monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
    monkeypatch.setattr(emit, "_retry_delay", lambda _attempts: 0.0)
    emit.reset_caches()
    try:
        CrewLog.create(lg.KIND_SESSION, "u-ack", owner="raymond", agent="kirocrew", slot=CONDUCTOR)
        good = {"slot": CONDUCTOR, "actor": "conductor", "by": CONDUCTOR, "action": "goal"}
        assert emit.on_work_recorded("u-ack", {**good, "goal": "ship it"}) is True
        assert emit.on_work_recorded("u-ack", {**good, "actor": "nobody"}, timeout=2.0) is False
        handle = projection.open_session_log("u-ack")
        assert handle is not None
        kinds = [e.type for e in handle.iter_from(1, known=projection.KNOWN_TYPES)]
        assert kinds.count("work/recorded") == 1
        monkeypatch.setenv(emit.CREW_LOG_ENV, "0")
        assert emit.on_work_recorded("u-ack", {**good, "goal": "again"}) is False
    finally:
        emit.drain_for_shutdown(timeout=2.0)
        emit.reset_caches()


# -- the fold is told its board; the rebuild leaves exactly the recorded board --


def test_a_nested_conductors_own_board_folds_even_when_its_first_entry_reports_up():
    """The nested conductor is a worker of PARENT and the conductor of NESTED. Its
    log's FIRST entry is a report to the parent's board; that entry must not pick
    the fold's board. Bound by the reader, the fold is of NESTED regardless."""
    from kiro_crew import crew_log as lg
    from kiro_crew.crew_log import CrewLog

    parent, nested = "chat-1-parent", "chat-2-nested"
    unit = CrewLog.create(
        lg.KIND_SESSION, "u-nested", owner="raymond", agent="kirocrew", slot=nested
    )
    unit.append(
        "work/recorded",
        {
            "slot": parent,
            "actor": "worker",
            "by": nested,
            "action": "report",
            "item_id": "it_0000aaaa",
            "status": "progress",
            "summary": "reporting up first",
            "event": "reporting up first",
            "event_kind": "report",
        },
        src="gateway",
    )
    mine = {"slot": nested, "actor": "conductor", "by": nested}
    unit.append(
        "work/recorded",
        {**mine, "action": "goal", "goal": "sub-goal", "round": 1, "depth": 1},
        src="gateway",
    )
    unit.append(
        "work/recorded",
        {
            **mine,
            "action": "create",
            "item_id": "it_0000bbbb",
            "title": "sub item",
            "event": "sub item",
            "event_kind": "create",
        },
        src="gateway",
    )

    board = projection.read_slot_projection(nested, "work").value
    assert board["conductor"]["slot_key"] == nested and board["conductor"]["goal"] == "sub-goal"
    assert [item["item_id"] for item in board["items"]] == ["it_0000bbbb"]
    # Unbound, the fold still refuses to take its board from a worker's report.
    unbound = projection.fold_slot("work", ["u-nested"]).value
    assert unbound["conductor"]["slot_key"] == nested
    assert [item["item_id"] for item in unbound["items"]] == ["it_0000bbbb"]


def test_rebuild_removes_an_item_file_the_fold_does_not_know(monkeypatch):
    from kiro_crew import crew_log as lg
    from kiro_crew.crew_log import CrewLog

    conductor = CrewLog.create(
        lg.KIND_SESSION, "u-conductor", owner="raymond", agent="kirocrew", slot=CONDUCTOR
    )
    mine = {"slot": CONDUCTOR, "actor": "conductor", "by": CONDUCTOR}
    conductor.append(
        "work/recorded", {**mine, "action": "goal", "goal": "ship it", "round": 1}, src="gateway"
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
    # A cache record with no entry behind it: written by hand, as damage would be.
    wl.ensure_conductor(CONDUCTOR, goal="ship it")
    stray = wl.item_path(CONDUCTOR, "it_0000dead")
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_text(json.dumps({"item_id": "it_0000dead", "title": "stray"}), encoding="utf-8")
    wl.item_events_path(CONDUCTOR, "it_0000dead").write_text("", encoding="utf-8")

    counts = wl.rebuild_from_projection(CONDUCTOR)
    assert counts == {"slot_key": CONDUCTOR, "items": 1, "events": 1, "removed": 1, "legacy": 0}
    assert not stray.exists() and not wl.item_events_path(CONDUCTOR, "it_0000dead").exists()
    assert wl.read_work_item(CONDUCTOR, "it_0000abcd") is not None


def test_rebuild_refuses_an_unstamped_post_epoch_item_missing_from_the_fold(monkeypatch):
    """A create committed after the projection epoch can be missing its
    ``recorded_at`` only inside the cache-commit/log-append crash window. The
    rebuild must not delete that cache-only item merely because its stamp never
    got a chance to land."""
    monkeypatch.setattr(wl, "_now_iso", lambda: "2026-09-20T10:00:01+00:00")
    wl.ensure_conductor(CONDUCTOR, goal="ship it")
    lost = wl.apply_conductor_action(
        CONDUCTOR, "create", title="committed before exit", acceptance=ACCEPTANCE
    )["item"]
    assert not lost.recorded_at
    monkeypatch.setattr(
        projection,
        "read_slot_projection",
        lambda slot, name, **_kw: SimpleNamespace(
            value={
                "conductor": {
                    "schema": 1,
                    "slot_key": slot,
                    "goal": "ship it",
                    "round": 1,
                    "depth": 0,
                    "parent_item": None,
                    "created_at": "2026-09-20T10:00:00+00:00",
                    "entries": 1,
                    "first_entry_at": "2026-09-20T10:00:00+00:00",
                },
                "items": [],
                "omitted": 0,
            }
        ),
    )

    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.rebuild_from_projection(CONDUCTOR)

    assert caught.value.code == wl.CODE_CREW_LOG_INCOMPLETE
    assert lost.item_id in str(caught.value)
    kept = wl.read_work_item(CONDUCTOR, lost.item_id)
    assert kept is not None and kept.title == "committed before exit"


# -- every action's committed fields; the store's widths; the unrecorded undo ---


@pytest.mark.asyncio
async def test_decide_and_accept_log_the_fields_they_commit(recorded):
    item_id = await _board()
    long_decision = "d" * 1500
    status, body = await _record(
        CONDUCTOR, {"action": "decide", "item_id": item_id, "decision": long_decision}
    )
    assert status == 200, body
    status, body = await _record(
        CONDUCTOR,
        {"action": "accept", "item_id": item_id, "acceptance": {"kind": "pr_checks", "pr": 5}},
    )
    assert status == 200, body

    decide, accept = (data for _unit, data in recorded[-2:])
    assert (decide["action"], decide["decision"]) == ("decide", long_decision)
    assert (accept["action"], accept["acceptance"]) == ("accept", {"kind": "pr_checks", "pr": 5})
    assert "title" not in decide and "decision" not in accept


def test_the_fold_keeps_text_at_the_stores_widths_not_the_shared_two_hundred():
    from kiro_crew import crew_log as lg
    from kiro_crew.crew_log import CrewLog

    unit = CrewLog.create(
        lg.KIND_SESSION, "u-conductor", owner="raymond", agent="kirocrew", slot=CONDUCTOR
    )
    mine = {"slot": CONDUCTOR, "actor": "conductor", "by": CONDUCTOR}
    unit.append(
        "work/recorded", {**mine, "action": "goal", "goal": "g" * 1200, "round": 1}, src="gateway"
    )
    unit.append(
        "work/recorded",
        {
            **mine,
            "action": "create",
            "item_id": "it_0000abcd",
            "title": "t" * 200,
            "event": "x",
            "event_kind": "create",
        },
        src="gateway",
    )
    unit.append(
        "work/recorded",
        {
            **mine,
            "action": "decide",
            "item_id": "it_0000abcd",
            "decision": "d" * 1500,
            "event": "x",
            "event_kind": "decision",
        },
        src="gateway",
    )
    board = projection.read_slot_projection(CONDUCTOR, "work").value
    assert len(board["conductor"]["goal"]) == 1200
    [item] = board["items"]
    assert (len(item["title"]), len(item["decision"])) == (200, 1500)


@pytest.mark.asyncio
async def test_an_unconfirmed_write_is_undone_from_the_record_in_the_same_request(monkeypatch):
    """Real units: the record holds goal+create; a report whose append is not
    confirmed is answered 503 and its cache mutation is dropped by the rebuild
    the refusal runs, so the cache never keeps what the log never saw."""
    from kiro_crew import crew_log as lg
    from kiro_crew.crew_log import CrewLog

    monkeypatch.setattr(routes.crew_log_emit, "enabled", lambda: True)
    monkeypatch.setattr(
        routes,
        "unit_for_session_key",
        lambda sessions, key: {CONDUCTOR: "u-conductor", WORKER: "u-worker"}[key],
    )
    CrewLog.create(
        lg.KIND_SESSION, "u-conductor", owner="raymond", agent="kirocrew", slot=CONDUCTOR
    )
    CrewLog.create(lg.KIND_SESSION, "u-worker", owner="raymond", agent="kirocrew", slot=WORKER)
    # The conductor's writes land in the record; the worker's append will not.
    monkeypatch.setattr(
        routes.crew_log_emit,
        "on_work_recorded",
        lambda unit, data: (
            bool(projection.open_session_log(unit).append("work/recorded", data, src="gateway"))
            if unit == "u-conductor"
            else False
        ),
    )
    item_id = await _board()
    before = wl.read_work_item(CONDUCTOR, item_id)
    assert before is not None and before.status is None

    status, body = await _report(WORKER, {"status": "progress", "summary": "never recorded"})
    assert (status, body["code"]) == (503, "crew_log_unrecorded"), body
    after = wl.read_work_item(CONDUCTOR, item_id)
    assert after is not None and after.status is None and after.summary == ""


@pytest.mark.asyncio
async def test_an_oversized_first_write_leaves_no_ledger_behind(recorded):
    """The fit probe runs before the ledger is bootstrapped: a refused first
    `create` leaves neither a conductor record nor a directory."""
    status, body = await _record(
        CONDUCTOR,
        {"action": "create", "title": "too big", "acceptance": {"text": "a" * 70_000}},
    )
    assert (status, body["code"]) == (400, "work_entry_too_large"), body
    assert wl.read_conductor(CONDUCTOR) is None
    assert not wl.conductor_dir(CONDUCTOR).exists()
    assert recorded == []


@pytest.mark.asyncio
async def test_a_legacy_item_too_large_to_record_whole_names_the_conductors_remedy(recorded):
    """A pre-projection item's first recorded write carries the whole item, so an
    item whose committed acceptance is wider than a log line cannot be written
    about at all -- and shortening the report changes nothing, because the report
    is not what overflows. The refusal must say so and name the one write that
    unsticks it: the conductor's ``accept`` with a smaller acceptance, whose entry
    carries the new acceptance over the baseline and so fits."""
    wl.ensure_conductor(CONDUCTOR, goal="old goal")
    old = wl.apply_conductor_action(
        CONDUCTOR, "create", title="old", acceptance={"text": "a" * 70_000}
    )["item"]
    _SLOTS[WORKER] = _Slot(created_by=CONDUCTOR)
    wl.apply_conductor_action(CONDUCTOR, "bind", item_id=old.item_id, worker_session_key=WORKER)
    before = len(recorded)

    status, body = await _report(WORKER, {"status": "progress", "summary": "tiny"})
    assert (status, body["code"], body["field"]) == (400, "work_item_too_large", "acceptance"), body
    assert "action=accept" in body["error"] and old.item_id in body["error"]
    # A conductor write that does not touch the acceptance is stuck the same way.
    status, body = await _record(
        CONDUCTOR, {"action": "decide", "item_id": old.item_id, "decision": "go on"}
    )
    assert (status, body["code"]) == (400, "work_item_too_large"), body
    assert len(recorded) == before
    stuck = wl.read_work_item(CONDUCTOR, old.item_id)
    assert stuck is not None and stuck.status is None and not stuck.recorded_at

    # The named remedy lands, records the item whole, and unsticks the worker.
    status, body = await _record(
        CONDUCTOR, {"action": "accept", "item_id": old.item_id, "acceptance": ACCEPTANCE}
    )
    assert status == 200, body
    assert len(recorded) == before + 1 and recorded[-1][1].get("baseline") is True
    assert recorded[-1][1]["acceptance"] == ACCEPTANCE
    assert wl.read_work_item(CONDUCTOR, old.item_id).recorded_at
    status, body = await _report(WORKER, {"status": "progress", "summary": "tiny"})
    assert status == 200, body

    # An oversized REQUEST keeps its own refusal: the request is what to shorten.
    status, body = await _record(
        CONDUCTOR,
        {"action": "accept", "item_id": old.item_id, "acceptance": {"text": "a" * 70_000}},
    )
    assert (status, body["code"]) == (400, "work_entry_too_large"), body


@pytest.mark.asyncio
async def test_the_fit_probe_is_never_narrower_than_the_entry_it_stands_for(recorded, monkeypatch):
    """Every key the committed entry carries is in the probe, at a value at least
    as wide -- the store-generated stamps and ids at their widest, and the
    baseline an unstamped item is owed. A probe missing any of them would let a
    near-limit write commit and be undone with a 503 where a 400 was promised."""
    probes = _capture_probes(monkeypatch)
    # A pre-projection item: unstamped, so its first recorded report owes a baseline.
    wl.ensure_conductor(CONDUCTOR, goal="old goal")
    old = wl.apply_conductor_action(CONDUCTOR, "create", title="old", acceptance=ACCEPTANCE)["item"]
    _SLOTS[WORKER] = _Slot(created_by=CONDUCTOR)
    wl.apply_conductor_action(CONDUCTOR, "bind", item_id=old.item_id, worker_session_key=WORKER)

    status, body = await _report(WORKER, {"status": "progress", "summary": "first recorded"})
    assert status == 200, body
    _unit, entry = recorded[-1]
    probe = probes[-1]
    assert entry["baseline"] is True and probe["baseline"] is True
    assert set(entry) <= set(probe), set(entry) - set(probe)
    assert _width(probe) >= _width(entry)

    # A create writes created_at and a generation the request never carried.
    status, body = await _record(
        CONDUCTOR, {"action": "create", "title": "new", "acceptance": ACCEPTANCE}
    )
    assert status == 200, body
    _unit, entry = recorded[-1]
    assert "created_at" in entry and "generation" in entry
    assert set(entry) <= set(probes[-1]), set(entry) - set(probes[-1])
    assert _width(probes[-1]) >= _width(entry)

    # A close stamps closed_at.
    status, body = await _record(
        CONDUCTOR, {"action": "close", "item_id": body["item"]["item_id"], "state": "abandoned"}
    )
    assert status == 200, body
    _unit, entry = recorded[-1]
    assert "closed_at" in entry
    assert set(entry) <= set(probes[-1]), set(entry) - set(probes[-1])
    assert _width(probes[-1]) >= _width(entry)


@pytest.mark.asyncio
async def test_a_legacy_items_first_conductor_write_keeps_the_whole_baseline(recorded):
    """The action passes ``None`` for every field it does not set; that must read
    as "unchanged", not "clear", or a legacy item whose first recorded mutation is
    a ``decide`` would be recorded without its title and acceptance and could
    never rebuild."""
    wl.ensure_conductor(CONDUCTOR, goal="old goal")
    old = wl.apply_conductor_action(CONDUCTOR, "create", title="old", acceptance=ACCEPTANCE)["item"]

    status, body = await _record(
        CONDUCTOR, {"action": "decide", "item_id": old.item_id, "decision": "go"}
    )
    assert status == 200, body
    _unit, entry = recorded[-1]
    assert entry["baseline"] is True
    assert (entry["title"], entry["acceptance"], entry["decision"]) == ("old", ACCEPTANCE, "go")
    assert entry["created_at"] == old.created_at
    assert wl.read_work_item(CONDUCTOR, old.item_id).recorded_at


@pytest.mark.asyncio
async def test_a_legacy_stored_baseline_is_redacted_before_recording(recorded):
    secret = "AKIAIOSFODNN7EXAMPLE"
    acceptance = {"kind": "human_approval", "notes": [f"inspect {secret}"]}
    wl.ensure_conductor(CONDUCTOR, goal="old goal")
    old = wl.apply_conductor_action(
        CONDUCTOR,
        "create",
        title=f"legacy item with {secret}",
        acceptance=acceptance,
    )["item"]
    assert secret in old.title
    assert secret in json.dumps(old.acceptance)

    once = routes.crew_log_emit.safe_work_fields({"title": old.title, "acceptance": old.acceptance})
    assert routes.crew_log_emit.safe_work_fields(once) == once

    status, body = await _record(
        CONDUCTOR, {"action": "decide", "item_id": old.item_id, "decision": "go"}
    )

    assert status == 200, body
    _unit, entry = recorded[-1]
    assert entry["baseline"] is True
    recorded_entry = json.dumps(entry, ensure_ascii=False)
    assert secret not in recorded_entry
    assert "[REDACTED:" in recorded_entry


@pytest.mark.asyncio
async def test_a_legacy_baseline_key_collision_is_refused_before_writing(recorded):
    first = "AKIAIOSFODNN7EXAMPLE"
    # Assembled rather than written out: the internal content scan flags an AKIA
    # literal in an added line, and only the well-known documentation key above is
    # allowlisted. The redactor sees a real credential shape at runtime, which is what
    # this test needs, while no line here carries one. Do not inline this back.
    second = "AKIA" + "Z" * 16
    wl.ensure_conductor(CONDUCTOR, goal="old goal")
    old = wl.apply_conductor_action(
        CONDUCTOR,
        "create",
        title="legacy item",
        acceptance={"kind": "human_approval", first: "one", second: "two"},
    )["item"]

    status, body = await _record(
        CONDUCTOR, {"action": "decide", "item_id": old.item_id, "decision": "go"}
    )

    assert (status, body["code"]) == (400, wl.CODE_INVALID_VALUE), body
    assert "collide after redaction" in body["error"]
    assert recorded == []
    kept = wl.read_work_item(CONDUCTOR, old.item_id)
    assert kept is not None and kept.decision == "" and not kept.recorded_at


# -- boards from before the projection; boards born in a failed request; bindings -


@pytest.mark.asyncio
async def test_items_from_before_the_projection_survive_a_rebuild(monkeypatch):
    """A v0.6-era board: two items written by the store with no crew-log entry.
    The board then takes ONE recorded write and is rebuilt: the recorded item is
    rebuilt, the pre-projection items and the old worker's binding stay."""
    import time

    wl.ensure_conductor(CONDUCTOR, goal="old goal")
    old_a = wl.apply_conductor_action(CONDUCTOR, "create", title="old a", acceptance=ACCEPTANCE)[
        "item"
    ]
    old_b = wl.apply_conductor_action(CONDUCTOR, "create", title="old b", acceptance=ACCEPTANCE)[
        "item"
    ]
    _SLOTS["chat-9-old-worker"] = _Slot(created_by=CONDUCTOR)
    wl.apply_conductor_action(
        CONDUCTOR, "bind", item_id=old_a.item_id, worker_session_key="chat-9-old-worker"
    )
    time.sleep(1.1)  # the first recorded entry must stamp a later second
    _real_units(monkeypatch)

    status, body = await _record(
        CONDUCTOR, {"action": "create", "title": "new one", "acceptance": ACCEPTANCE}
    )
    assert status == 200, body
    new_id = body["item"]["item_id"]

    counts = wl.rebuild_from_projection(CONDUCTOR)
    assert (counts["items"], counts["removed"], counts["legacy"]) == (1, 0, 2)
    assert wl.read_work_item(CONDUCTOR, old_a.item_id) is not None
    assert wl.read_work_item(CONDUCTOR, old_b.item_id) is not None
    assert wl.read_work_item(CONDUCTOR, new_id) is not None
    assert wl.read_binding("chat-9-old-worker") == (CONDUCTOR, old_a.item_id)
    header = wl.read_conductor(CONDUCTOR)
    assert header is not None and header.goal == "old goal"
    # The counter covers every record the rebuilt board holds: a legacy item's
    # first recorded mutation materialises it in the fold, where it takes a slot.
    assert header.created_total == counts["items"] + counts["legacy"] == 3


@pytest.mark.asyncio
async def test_a_present_but_unreadable_record_is_not_deleted_by_a_rebuild(monkeypatch):
    """A transient read error on a cache record the fold does not name must not delete
    it. The rebuild reads such records strictly, so an I/O blip (a Windows rename race,
    a permission hiccup) RAISES and unwinds the rebuild's undo instead of reading the
    file as absent and unlinking it -- which would turn a momentary read error into
    real data loss. The record stays on disk for the next rebuild."""
    _real_units(monkeypatch)
    wl.ensure_conductor(CONDUCTOR, goal="goal")

    # A real recorded create, so the fold has entries and the rebuild runs its
    # post-write pass over the stray below (an empty fold short-circuits the rebuild).
    status, body = await _record(
        CONDUCTOR, {"action": "create", "title": "recorded one", "acceptance": ACCEPTANCE}
    )
    assert status == 200, body

    # A stray record on disk the fold does not name, so the post-write pass reads it.
    stray_id = "it_0000beef"
    stray = wl.item_path(CONDUCTOR, stray_id)
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_text(json.dumps({"item_id": stray_id, "title": "stray"}), encoding="utf-8")

    real_read = wl.read_work_item

    def _blip_on_strict(slot: str, item_id: str, *, strict: bool = False):
        if item_id == stray_id and strict:
            raise OSError("permission blip, present but unreadable")
        return real_read(slot, item_id, strict=strict)

    monkeypatch.setattr(wl, "read_work_item", _blip_on_strict)

    with pytest.raises(OSError):
        wl.rebuild_from_projection(CONDUCTOR)

    # The undo restored it: the record is still on disk, byte for byte.
    assert stray.exists()
    assert json.loads(stray.read_text(encoding="utf-8"))["item_id"] == stray_id


@pytest.mark.asyncio
async def test_a_board_born_in_a_failed_request_leaves_nothing_behind(monkeypatch):
    _real_units(monkeypatch)
    monkeypatch.setattr(routes.crew_log_emit, "on_work_recorded", lambda unit, data: False)

    status, body = await _record(
        CONDUCTOR, {"action": "create", "title": "first ever", "acceptance": ACCEPTANCE}
    )
    assert (status, body["code"]) == (503, "crew_log_unrecorded"), body
    assert wl.read_conductor(CONDUCTOR) is None
    assert wl.read_work_item(CONDUCTOR, body["item_id"]) is None


@pytest.mark.asyncio
async def test_bindings_follow_the_record_on_rebuild(monkeypatch):
    """A bind whose append did not land: the cache shows the worker bound, the
    record does not. The rebuild rewrites the item without the worker and
    removes the worker's binding; a recorded bind is written back if missing."""
    _real_units(monkeypatch)
    item_id = await _board()  # goal, create, bind WORKER -- all recorded
    assert wl.read_binding(WORKER) == (CONDUCTOR, item_id)

    # A second item bound to another worker, whose bind entry never lands.
    status, body = await _record(
        CONDUCTOR, {"action": "create", "title": "item two", "acceptance": ACCEPTANCE}
    )
    assert status == 200, body
    second = body["item"]["item_id"]
    _SLOTS["chat-9-other"] = _Slot(created_by=CONDUCTOR)
    real = routes.crew_log_emit.on_work_recorded
    monkeypatch.setattr(routes.crew_log_emit, "on_work_recorded", lambda unit, data: False)
    status, body = await _record(
        CONDUCTOR, {"action": "bind", "item_id": second, "worker_session_key": "chat-9-other"}
    )
    assert (status, body["code"]) == (503, "crew_log_unrecorded"), body
    monkeypatch.setattr(routes.crew_log_emit, "on_work_recorded", real)

    # The compensation already ran; the unrecorded bind is gone from cache and binding.
    two = wl.read_work_item(CONDUCTOR, second)
    assert two is not None and not two.worker_session_key
    assert wl.read_binding("chat-9-other") is None
    # And a recorded binding that went missing from disk is written back.
    wl.binding_path(WORKER).unlink()
    wl.rebuild_from_projection(CONDUCTOR)
    assert wl.read_binding(WORKER) == (CONDUCTOR, item_id)


@pytest.mark.asyncio
async def test_a_read_never_publishes_a_mutation_the_record_then_rolls_back(recorded, monkeypatch):
    """A write commits to the cache, then appends to the record in a thread, and
    undoes the commit when the append fails. A read landing between the two
    steps would serve a status the record never holds and that is about to be
    rolled back; both read routes therefore wait on the board lock the write
    holds, and see the board only as the record leaves it."""
    import asyncio
    import threading

    item_id = await _board()
    before = json.loads(
        (await routes.api_work_brief(_req("GET", "/api/work-ledger/brief", sk=WORKER))).text
    )["brief"]

    entered = threading.Event()
    release = threading.Event()

    def _held_append(unit: str, data: dict[str, Any]) -> bool:
        entered.set()
        release.wait(5)
        return False

    monkeypatch.setattr(routes.crew_log_emit, "on_work_recorded", _held_append)
    report = asyncio.ensure_future(
        _report(
            WORKER,
            {"status": "done", "summary": "never landed", "artifacts": {"commit": "abc1234"}},
        )
    )
    # The commit has happened and the append is in flight: the window.
    assert await asyncio.to_thread(entered.wait, 5)
    assert wl.read_work_item(CONDUCTOR, item_id).status == "done"

    brief = asyncio.ensure_future(
        routes.api_work_brief(_req("GET", "/api/work-ledger/brief", sk=WORKER))
    )
    ledger = asyncio.ensure_future(
        routes.api_work_ledger_get(_req("GET", "/api/work-ledger", sk=CONDUCTOR))
    )
    # Both reads reach the board lock the write holds and park on it: the barrier
    # before asserting that neither ran inside the window.
    board_lock, _ = routes._BOARD_LOCKS._locks[CONDUCTOR]
    await until_parked(
        board_lock,
        count=2,
        describe=lambda: f"brief done={brief.done()}, ledger done={ledger.done()}",
    )
    assert not brief.done() and not ledger.done(), "a read ran inside the write's window"

    release.set()
    status, body = await report
    assert (status, body["code"]) == (503, "crew_log_unrecorded"), body

    brief_body = json.loads((await brief).text)["brief"]
    assert (brief_body["status"], brief_body["summary"]) == (before["status"], before["summary"])
    ledger_body = json.loads((await ledger).text)
    (row,) = [r for r in ledger_body["items"] if r["item_id"] == item_id]
    assert (row["status"], row["summary"]) == (before["status"], before["summary"])
    assert row["status"] != "done"
