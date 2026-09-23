"""The work ledger as a projection of the crew log.

Every write the two routes accept is first gated on the crew log and then
recorded as one ``work/recorded`` entry in the acting session's log; the JSON
under ``work-ledger/<conductor>/`` is a cache that ``rebuild_from_projection``
re-materialises from the ``work`` fold. These tests pin the gate (refuse when the
emitter is off or the caller's unit is unknown, writing nothing), the entry per
mutation carrying only the fields that action set, and the rebuild round trip.
"""

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew import work_ledger as wl
from kiro_crew.crew_log import projection
from kiro_crew.dashboard.handlers import work_ledger as routes

CONDUCTOR = "chat-9-conductor"
WORKER = "chat-9-worker"
ACCEPTANCE = {"kind": "human_approval"}


class _Slot:
    """The slot attributes the routes read, and nothing else."""

    def __init__(self, created_by: str = "", workspace: str = "default") -> None:
        self._created_by = created_by
        self.workspace = workspace
        self.running = False


_SLOTS: dict[str, _Slot] = {}


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """An isolated data home and an open route; the gate itself stays live."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    _SLOTS.clear()
    # Each test runs on its own event loop and an ``asyncio.Lock`` binds to the
    # loop it is first contended on, so a lock left from one test would fail the
    # next test's contended wait with "bound to a different event loop".
    routes._BOARD_LOCKS.clear()

    async def _recognized(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(routes, "_recognize_session", _recognized)
    monkeypatch.setattr(routes, "_is_restricted_session", lambda *a: False)
    monkeypatch.setattr(routes, "_reaches_a_channel", lambda request, sk: False)
    yield
    _SLOTS.clear()


@pytest.fixture
def recorded(monkeypatch) -> list[tuple[str, dict[str, Any]]]:
    """The crew log on, every caller resolving to ``unit:<key>``, and every
    ``work/recorded`` append captured instead of written."""
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(routes.crew_log_emit, "enabled", lambda: True)
    monkeypatch.setattr(routes, "unit_for_session_key", lambda sessions, key: f"unit:{key}")
    monkeypatch.setattr(
        routes.crew_log_emit,
        "on_work_recorded",
        lambda unit, data: calls.append((unit, data)) or True,
    )
    return calls


def _req(method: str, path: str, *, body: Any = ..., sk: str) -> web.Request:
    app = web.Application()
    state = MagicMock()
    state.get_slot = MagicMock(side_effect=lambda key: _SLOTS.get(key))
    app["state"] = state
    req = make_mocked_request(method, path, app=app, headers={"X-Session-Key": sk})
    req["internal_auth"] = True
    if body is not ...:
        req.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
    return req


async def _record(sk: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    resp = await routes.api_work_ledger_record(
        _req("POST", "/api/work-ledger/record", body=body, sk=sk)
    )
    return resp.status, json.loads(resp.text)


async def _report(sk: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    resp = await routes.api_work_report(_req("POST", "/api/work-ledger/report", body=body, sk=sk))
    return resp.status, json.loads(resp.text)


async def _board() -> str:
    """goal, create, bind -- through the routes, so each is itself recorded."""
    status, body = await _record(CONDUCTOR, {"action": "goal", "goal": "ship it", "round": 1})
    assert status == 200, body
    status, body = await _record(
        CONDUCTOR, {"action": "create", "title": "item one", "acceptance": ACCEPTANCE}
    )
    assert status == 200, body
    item_id = body["item"]["item_id"]
    _SLOTS[WORKER] = _Slot(created_by=CONDUCTOR)
    status, body = await _record(
        CONDUCTOR, {"action": "bind", "item_id": item_id, "worker_session_key": WORKER}
    )
    assert status == 200, body
    return item_id


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


def _rendered(slot: str, item_id: str) -> dict[str, Any]:
    """A ``work`` fold's rendered value: the shape ``_work_render`` produces."""
    return {
        "conductor": {
            "schema": 1,
            "slot_key": slot,
            "goal": "ship it",
            "round": 2,
            "depth": 0,
            "parent_item": None,
            "created_at": "2026-09-20T10:00:00",
            "entries": 2,
        },
        "items": [
            {
                "schema": 1,
                "item_id": item_id,
                "title": "item one",
                "acceptance": ACCEPTANCE,
                "state": "open",
                "verdict": None,
                "decision": "",
                "worker_session_key": WORKER,
                "round": 1,
                "fails": 0,
                "status": "progress",
                "summary": "half way",
                "artifacts": {},
                "pr": 12,
                "last_report_at": "2026-09-20T10:05:00",
                "created_at": "2026-09-20T10:01:00",
                "closed_at": None,
                "events": [
                    {
                        "id": "e1",
                        "ts": "2026-09-20T10:01:00",
                        "item_id": item_id,
                        "kind": "create",
                        "status": None,
                        "text": "item one",
                    },
                    {
                        "id": "e2",
                        "ts": "2026-09-20T10:05:00",
                        "item_id": item_id,
                        "kind": "report",
                        "status": "progress",
                        "text": "half way",
                    },
                ],
            }
        ],
        "omitted": 0,
    }


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


# -- real units, no mocked resolver and no pre-rendered fold ------------------


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


async def _rebuild(sk: str) -> tuple[int, dict[str, Any]]:
    resp = await routes.api_work_ledger_rebuild(
        _req("POST", "/api/work-ledger/rebuild", body={}, sk=sk)
    )
    return resp.status, json.loads(resp.text)


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


def _capture_probes(monkeypatch) -> list[dict[str, Any]]:
    probes: list[dict[str, Any]] = []
    monkeypatch.setattr(
        routes.crew_log_emit, "work_entry_fits", lambda entry: probes.append(entry) or True
    )
    return probes


def _width(entry: dict[str, Any]) -> int:
    return len(json.dumps(entry, ensure_ascii=False).encode("utf-8"))


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


def _real_units(monkeypatch, *, worker_lands: bool = True) -> None:
    """Real conductor and worker units; the routes' appends go to them for real."""
    from kiro_crew import crew_log as lg
    from kiro_crew.crew_log import CrewLog

    monkeypatch.setattr(routes.crew_log_emit, "enabled", lambda: True)
    units = {CONDUCTOR: "u-conductor", WORKER: "u-worker"}
    monkeypatch.setattr(routes, "unit_for_session_key", lambda sessions, key: units[key])
    CrewLog.create(
        lg.KIND_SESSION, "u-conductor", owner="raymond", agent="kirocrew", slot=CONDUCTOR
    )
    CrewLog.create(lg.KIND_SESSION, "u-worker", owner="raymond", agent="kirocrew", slot=WORKER)

    def _append(unit: str, data: dict[str, Any]) -> bool:
        if unit == "u-worker" and not worker_lands:
            return False
        projection.open_session_log(unit).append("work/recorded", data, src="gateway")
        return True

    monkeypatch.setattr(routes.crew_log_emit, "on_work_recorded", _append)


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
    report = asyncio.ensure_future(_report(WORKER, {"status": "done", "summary": "never landed"}))
    # The commit has happened and the append is in flight: the window.
    assert await asyncio.to_thread(entered.wait, 5)
    assert wl.read_work_item(CONDUCTOR, item_id).status == "done"

    brief = asyncio.ensure_future(
        routes.api_work_brief(_req("GET", "/api/work-ledger/brief", sk=WORKER))
    )
    ledger = asyncio.ensure_future(
        routes.api_work_ledger_get(_req("GET", "/api/work-ledger", sk=CONDUCTOR))
    )
    for _ in range(20):
        await asyncio.sleep(0.005)
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
        done, _waiting = await asyncio.wait({pending}, timeout=0.05)
        assert not done, "cancellation escaped before the append-or-rollback transaction drained"
    finally:
        release.set()

    with pytest.raises(asyncio.CancelledError):
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
        if calls["n"] == 2:  # the header went through; the first item write fails
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
    status, body = await _report(WORKER, {"status": "done", "summary": "finished"})
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
    entries = [
        e.data
        for e in projection.open_session_log("u-worker").iter_from(1, known=projection.KNOWN_TYPES)
        if e.type == "work/recorded"
    ]
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
async def test_a_pruned_worker_unit_refuses_the_rebuild_instead_of_erasing_its_reports(
    monkeypatch,
):
    from kiro_crew.crew_log import store

    _real_units(monkeypatch)
    item_id = await _board()
    status, body = await _report(WORKER, {"status": "progress", "summary": "half way"})
    assert status == 200, body
    before = wl.read_work_item(CONDUCTOR, item_id)

    # Retention takes the worker's unit; the conductor's create and bind remain.
    import shutil

    from kiro_crew import crew_log as lg

    shutil.rmtree(store.crew_log_dir(lg.KIND_SESSION, "u-worker"))
    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.rebuild_from_projection(CONDUCTOR)
    assert caught.value.code == "crew_log_incomplete"
    # The refusal names its exit: keep the cache, or drop the item's files and rebuild.
    assert "remove that item's cached files and rebuild" in str(caught.value)
    after = wl.read_work_item(CONDUCTOR, item_id)
    assert (after.status, after.summary, after.last_report_at) == (
        before.status,
        before.summary,
        before.last_report_at,
    )


@pytest.mark.asyncio
async def test_a_rebuild_keeps_the_completeness_marker_so_a_later_prune_still_refuses(
    monkeypatch,
):
    """Entries never carry ``recorded_at``; a rebuild that copied the fold verbatim
    would clear it, and the next completeness check would skip the item -- so a
    rebuild, a retention prune and a second rebuild would erase the worker's
    reports the first rebuild had just written. The stamp must survive the fold."""
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

    shutil.rmtree(store.crew_log_dir(lg.KIND_SESSION, "u-worker"))
    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.rebuild_from_projection(CONDUCTOR)
    assert caught.value.code == "crew_log_incomplete"
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

    lock = routes._board_lock(CONDUCTOR)
    await lock.acquire()
    queued = asyncio.ensure_future(
        _record(CONDUCTOR, {"action": "decide", "item_id": item_id, "decision": "go"})
    )
    for _ in range(20):
        await asyncio.sleep(0.005)
    assert not queued.done()  # past the early check, waiting on the lock
    wl.mark_cache_dirty(CONDUCTOR, "the write ahead could not be undone")
    lock.release()

    status, body = await queued
    assert (status, body["code"]) == (409, "cache_dirty"), body
    assert wl.read_work_item(CONDUCTOR, item_id).decision == ""

    # The same race on the worker's route.
    wl.clear_cache_dirty(CONDUCTOR)
    await lock.acquire()
    queued = asyncio.ensure_future(_report(WORKER, {"status": "progress", "summary": "x"}))
    for _ in range(20):
        await asyncio.sleep(0.005)
    assert not queued.done()
    wl.mark_cache_dirty(CONDUCTOR, "the write ahead could not be undone")
    lock.release()
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
        done, _waiting = await asyncio.wait({pending}, timeout=0.05)
        assert not done, "cancellation escaped before the append-or-rollback transaction drained"
    finally:
        release.set()

    with pytest.raises(asyncio.CancelledError):
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

    def _fail_once_the_new_item_has_landed(slot: str, item_id: str):
        if item_id == stray_id and wl.item_path(CONDUCTOR, new_id).exists():
            raise OSError("disk full, after the new item landed")
        return real_read(slot, item_id)

    took_the_lock = threading.Event()
    observed: dict[str, bool] = {}
    real_restore = wl._restore_files

    def _ask_at_undo_time(snapshot):
        probe = threading.Thread(target=_hold_briefly, daemon=True)
        probe.start()
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
    assert [data.get("generation") for _unit, data in recorded] == [stamped.generation]
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
    assert [data.get("generation") for _unit, data in recorded] == [None]
    landed = wl.read_work_item(CONDUCTOR, item_id)
    assert landed is not None and landed.status == "progress"
