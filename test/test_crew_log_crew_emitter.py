"""The crew emitter: the first writer for ``crew-log/crews/<store>/``.

Two halves, because the emitter and its call sites can fail independently.

The EMITTER half drives ``on_crew_dispatch`` / ``on_crew_report`` directly against
real files: where the entry lands, which ``src`` signs it, that a report's required
``ref`` is built by the writer rather than asked of the caller, that the report
threads onto the dispatch it answers, and that the flag being off leaves no
directory behind.

The CALL-SITE half drives the two work-ledger routes and asserts the entries
appear, mirroring ``test_work_ledger_projection.py``'s harness: the board's own
``work/recorded`` append is captured rather than written (its own suite covers it),
so what these tests measure is only the crew-side record.

One test exists specifically to pin a design decision that is invisible from the
entries alone -- the emitter caches no handle, so the write lease is free between
entries and ``remove_unit`` can still claim it ``sole``. A cached handle would hold
that lease for the process's life and make a crew's log un-removable.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog, emit
from kiro_crew.crew_log import schema as crew_schema
from kiro_crew.crew_log.store import REMOVE_REMOVED, remove_unit
from kiro_crew.dashboard.handlers import work_ledger as routes

CREW = "qa"
#: A member DM slot key, which is what a conductor slot looks like when the board
#: belongs to a crew. ``_crew_store`` folds it to the slug.
CONDUCTOR = f"member-{CREW}"
WORKER = "chat-9-worker"
WORKER_UNIT = f"unit:{WORKER}"
CONDUCTOR_UNIT = f"unit:{CONDUCTOR}"
ACCEPTANCE = {"kind": "human_approval"}


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """An isolated data home, the flag on, and the emitter's caches cleared."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
    emit.reset_caches()
    yield
    emit.reset_caches()


def _crew_dir():
    return lg.crew_log_dir(lg.KIND_CREW, CREW)


def _crew_entries() -> list[dict[str, Any]]:
    path = lg.crew_log_path(lg.KIND_CREW, CREW)
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines[1:] if line]


def _crew_header() -> dict[str, Any]:
    path = lg.crew_log_path(lg.KIND_CREW, CREW)
    return json.loads(path.read_text(encoding="utf-8").splitlines()[0])


def _seed_session(unit_id: str = WORKER_UNIT, entries: int = 3) -> CrewLog:
    """A session unit with *entries* lines, so a report has something to cite."""
    log = CrewLog.create(lg.KIND_SESSION, unit_id, owner=CREW, agent="kirocrew")
    for turn in range(1, entries + 1):
        log.append("turn/started", {"turn": turn, "actor": "user", "depth": 0}, src="gateway")
    return log


DISPATCH_DATA = {"item": "it_1", "target": {"kind": "session", "slot": WORKER}}


# --- the emitter: where the entry lands -----------------------------------


def test_a_dispatch_creates_the_crew_unit_under_the_crews_root():
    assert not _crew_dir().exists()
    seq = emit.on_crew_dispatch(CREW, DISPATCH_DATA)
    assert seq == 1
    # The store lives under the crews root, not the sessions one: the two kinds
    # share one fenced ``crew-log`` leaf and nothing else.
    path = lg.crew_log_path(lg.KIND_CREW, CREW)
    assert path.is_file()
    assert "crews" in path.parts and "sessions" not in path.parts
    assert _crew_header()["type"] == lg.KIND_CREW
    assert _crew_header()["id"] == CREW


def test_a_crew_that_dispatches_nothing_gets_no_crew_log():
    """The unit is created by the first fact worth recording, never eagerly.

    A session's log is created by the turn path, which knows the session is real.
    A crew has no such moment, so the first dispatch is it -- and a crew that never
    dispatches costs no file.
    """
    emit.on_crew_report(CREW, {"item": "it_1", "status": "done"}, cite_unit=WORKER_UNIT)
    assert not _crew_dir().exists()


def test_a_dispatch_is_signed_by_the_crew_whose_log_it_is():
    emit.on_crew_dispatch(CREW, DISPATCH_DATA)
    entry = _crew_entries()[0]
    assert entry["src"] == f"crew:{CREW}" == emit.crew_src(CREW)
    assert entry["type"] == emit.CREW_DISPATCH
    assert entry["data"] == DISPATCH_DATA
    # An opener carries no thread and no ref: nothing precedes it and it cites
    # nothing.
    assert "thread" not in entry and "ref" not in entry


def test_a_report_is_signed_by_the_gateway_not_by_a_crew():
    """The reporting party is a SESSION, and the gateway writes a session's report.

    ``crew:<name>`` would claim a crew reported, which is a different fact and a
    different reader attribution.
    """
    _seed_session()
    emit.on_crew_dispatch(CREW, DISPATCH_DATA)
    emit.on_crew_report(CREW, {"item": "it_1", "status": "done"}, cite_unit=WORKER_UNIT)
    assert _crew_entries()[1]["src"] == "gateway"


# --- the emitter: the report's evidence and its anchor --------------------


def test_the_report_builds_its_required_ref_from_the_cited_unit():
    _seed_session(entries=3)
    emit.on_crew_dispatch(CREW, DISPATCH_DATA)
    emit.on_crew_report(CREW, {"item": "it_1", "status": "done"}, cite_unit=WORKER_UNIT)
    ref = _crew_entries()[1]["ref"]
    # The whole of the cited unit, because three lines fit the cap.
    assert ref == {"unit": lg.KIND_SESSION, "id": WORKER_UNIT, "from": 1, "to": 3}


def test_the_cited_span_is_clamped_to_the_cap_rather_than_refused(monkeypatch):
    """A long run is cited by its relevant span, which is what the cap is for.

    Patched on both names the value is read through: the emitter reads it off the
    package and ``Ref`` enforces it from the schema module, so a test that patched
    one would measure the other.
    """
    monkeypatch.setattr(crew_schema, "MAX_REF_SPAN", 2)
    monkeypatch.setattr(lg, "MAX_REF_SPAN", 2, raising=False)
    _seed_session(entries=5)
    emit.on_crew_dispatch(CREW, DISPATCH_DATA)
    emit.on_crew_report(CREW, {"item": "it_1", "status": "done"}, cite_unit=WORKER_UNIT)
    ref = _crew_entries()[1]["ref"]
    assert ref["from"] == 4 and ref["to"] == 5


@pytest.mark.parametrize("cite_unit", ["", "unit:never-written"])
def test_a_report_with_no_evidence_to_cite_is_not_written(cite_unit):
    """``ref`` is required, so a report that cannot carry one is not written.

    A report is a claim about work that happened elsewhere, and the ``ref`` is what
    makes the claim checkable. Nothing later can attach evidence to a line that is
    already written, so an unfalsifiable report is permanent -- better absent.
    """
    emit.on_crew_dispatch(CREW, DISPATCH_DATA)
    assert emit.on_crew_report(CREW, {"item": "it_1", "status": "done"}, cite_unit=cite_unit) == 0
    assert [entry["type"] for entry in _crew_entries()] == [emit.CREW_DISPATCH]


def test_a_report_threads_onto_the_dispatch_for_its_own_item():
    """Read from the log, not remembered, so it survives a restart between the two."""
    _seed_session()
    first = emit.on_crew_dispatch(CREW, {"item": "it_1", "target": {"kind": "crew", "name": "a"}})
    second = emit.on_crew_dispatch(CREW, {"item": "it_2", "target": {"kind": "crew", "name": "b"}})
    emit.on_crew_report(CREW, {"item": "it_2", "status": "progress"}, cite_unit=WORKER_UNIT)
    emit.on_crew_report(CREW, {"item": "it_1", "status": "done"}, cite_unit=WORKER_UNIT)
    threads = [entry.get("thread") for entry in _crew_entries()]
    assert threads == [None, None, second, first]


def test_a_report_with_no_dispatch_behind_it_carries_no_thread():
    _seed_session()
    emit.on_crew_dispatch(CREW, DISPATCH_DATA)
    emit.on_crew_report(
        CREW, {"item": "it_other", "status": "blocked"}, cite_unit=WORKER_UNIT, replying=False
    )
    assert "thread" not in _crew_entries()[1]


def test_an_answer_whose_dispatch_is_missing_is_not_written_at_all():
    """A reply that cannot resolve its anchor must not be written as volunteered.

    The dispatch append is best effort, so a transient failure there leaves a crew
    log with no dispatch to thread onto while the later report's own append
    succeeds -- two separate requests. Writing the reply anyway records the wrong
    provenance, and the spec reads a missing ``thread`` as volunteered, so the file
    would state a fact no writer meant and nothing can correct. ``replying`` is
    what tells the two apart, which is why it is the caller's to state.
    """
    _seed_session()
    # The dispatch that would have been the anchor never landed.
    assert (
        emit.on_crew_report(
            CREW, {"item": "it_1", "status": "done"}, cite_unit=WORKER_UNIT, replying=True
        )
        == 0
    )
    assert _crew_entries() == []


def test_the_default_fails_closed_so_a_caller_that_says_nothing_loses_the_entry():
    """Defaulting to ``replying`` costs an entry a later report replaces.

    The other default would commit a claim about where the work came from that
    nothing rewrites, so the cheap failure is the right one to pick.
    """
    _seed_session()
    assert emit.on_crew_report(CREW, {"item": "it_1", "status": "done"}, cite_unit=WORKER_UNIT) == 0
    assert _crew_entries() == []


def test_a_dispatch_older_than_the_window_is_still_found_by_the_fallback_scan(monkeypatch):
    """A window miss must not record the report as volunteered.

    ``thread`` absent is not a weaker version of the right answer: the spec reads
    it as a report with no dispatch behind it, so a reply whose anchor fell out of
    the window would state a different fact, permanently. The window is the fast
    path; the file is the answer.
    """
    monkeypatch.setattr(emit, "_THREAD_LOOKBACK", 2)
    _seed_session()
    anchor = emit.on_crew_dispatch(CREW, DISPATCH_DATA)
    for name in ("a", "b", "c"):
        emit.on_crew_dispatch(
            CREW, {"item": f"it_{name}", "target": {"kind": "crew", "name": name}}
        )
    emit.on_crew_report(CREW, {"item": "it_1", "status": "done"}, cite_unit=WORKER_UNIT)
    assert _crew_entries()[-1]["thread"] == anchor


def test_the_window_answers_without_scanning_when_the_dispatch_is_recent(monkeypatch):
    """The fast path is the point of the window, so it is pinned separately."""
    monkeypatch.setattr(emit, "_THREAD_LOOKBACK", 2)
    _seed_session()
    anchor = emit.on_crew_dispatch(CREW, DISPATCH_DATA)
    calls: list[int] = []
    real = emit._newest_dispatch_for

    def counted(log, item, start):
        calls.append(start)
        return real(log, item, start)

    monkeypatch.setattr(emit, "_newest_dispatch_for", counted)
    emit.on_crew_report(CREW, {"item": "it_1", "status": "done"}, cite_unit=WORKER_UNIT)
    assert _crew_entries()[-1]["thread"] == anchor
    assert calls == [1], f"one lookup, and no second pass from seq 1: {calls}"


def test_a_report_for_an_item_no_dispatch_named_stays_unthreaded(monkeypatch):
    """The fallback scan reaching seq 1 and finding nothing is still no thread."""
    monkeypatch.setattr(emit, "_THREAD_LOOKBACK", 2)
    _seed_session()
    emit.on_crew_dispatch(CREW, DISPATCH_DATA)
    for name in ("a", "b", "c"):
        emit.on_crew_dispatch(
            CREW, {"item": f"it_{name}", "target": {"kind": "crew", "name": name}}
        )
    emit.on_crew_report(
        CREW, {"item": "it_absent", "status": "done"}, cite_unit=WORKER_UNIT, replying=False
    )
    assert "thread" not in _crew_entries()[-1]


# --- the emitter: what it refuses -----------------------------------------


#: A target has to name exactly ONE party. The registry cannot state a conditional
#: requirement, so each of these is the writer's refusal.
BAD_TARGETS: tuple[dict[str, Any], ...] = (
    {"item": "it_1"},
    {"item": "it_1", "target": {}},
    {"item": "it_1", "target": "dashboard:7"},
    {"item": "it_1", "target": {"kind": "session"}},
    {"item": "it_1", "target": {"kind": "crew"}},
    {"item": "it_1", "target": {"kind": "app", "slot": "dashboard:7"}},
    # Both forms at once names two parties, so it names neither definitely.
    {"item": "it_1", "target": {"kind": "session", "slot": "dashboard:7", "name": "a"}},
    {"item": "it_1", "target": {"kind": "crew", "name": "a", "slot": "dashboard:7"}},
)


@pytest.mark.parametrize("data", BAD_TARGETS)
def test_a_dispatch_whose_target_names_no_single_party_is_not_written(data):
    assert emit.on_crew_dispatch(CREW, data) == 0
    assert not _crew_dir().exists()


def test_both_legal_target_forms_are_written():
    assert emit.on_crew_dispatch(CREW, DISPATCH_DATA) == 1
    assert (
        emit.on_crew_dispatch(CREW, {"item": "it_2", "target": {"kind": "crew", "name": "a"}}) == 2
    )


def test_a_malformed_payload_is_refused_without_failing_the_caller():
    """Best effort: the board's own record is the authority, this one is beside it.

    A refusal answers ``0`` -- "not recorded" -- rather than raising into a route
    whose ledger write has already succeeded.
    """
    _seed_session()
    emit.on_crew_dispatch(CREW, DISPATCH_DATA)
    before = lg.crew_log_path(lg.KIND_CREW, CREW).read_bytes()
    # ``status`` is a closed enum, so the registry refuses this on the append path.
    # The session is seeded and the dispatch is present so the evidence and anchor
    # checks both pass: what answers here is the registry, not an earlier refusal.
    assert emit.on_crew_report(CREW, {"item": "it_1", "status": "nope"}, cite_unit=WORKER_UNIT) == 0
    assert lg.crew_log_path(lg.KIND_CREW, CREW).read_bytes() == before


# --- the emitter: the flag, and the lease ---------------------------------


def test_the_flag_being_off_writes_nothing_and_creates_no_directory(monkeypatch):
    monkeypatch.delenv(emit.CREW_LOG_ENV, raising=False)
    _seed_session()
    assert emit.on_crew_dispatch(CREW, DISPATCH_DATA) == 0
    assert emit.on_crew_report(CREW, {"item": "it_1", "status": "done"}, cite_unit=WORKER_UNIT) == 0
    assert not _crew_dir().exists()


def test_the_emitter_holds_no_lease_between_entries():
    """No handle is cached, so a unit nothing is writing stays removable.

    ``remove_unit`` claims the write lease SOLE, which a cached handle's own share
    of the refcount would refuse for the process's life. This is the assertion that
    the decision not to cache is real rather than intended.
    """
    emit.on_crew_dispatch(CREW, DISPATCH_DATA)
    assert remove_unit(lg.KIND_CREW, CREW, guard=lambda directory: True) == REMOVE_REMOVED
    assert not _crew_dir().exists()
    # And the next entry re-creates the unit rather than failing on the gap.
    assert emit.on_crew_dispatch(CREW, DISPATCH_DATA) == 1


# --- the two call sites ---------------------------------------------------


class _Slot:
    """The slot attributes the routes read, and nothing else."""

    def __init__(self, created_by: str = "", workspace: str = "default") -> None:
        self._created_by = created_by
        self.workspace = workspace
        self.running = False


_SLOTS: dict[str, _Slot] = {}


@pytest.fixture
def open_routes(monkeypatch) -> None:
    """The routes reachable, with the board's own append captured not written.

    ``work/recorded`` has its own suite; what these tests measure is the crew-side
    entry, so the session-kind append is stubbed out and every caller resolves to a
    predictable unit.
    """
    _SLOTS.clear()
    routes._BOARD_LOCKS.clear()

    async def _recognized(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(routes, "_recognize_session", _recognized)
    monkeypatch.setattr(routes, "_is_restricted_session", lambda *a: False)
    monkeypatch.setattr(routes, "_reaches_a_channel", lambda request, sk: False)
    monkeypatch.setattr(routes, "unit_for_session_key", lambda sessions, key: f"unit:{key}")
    monkeypatch.setattr(routes.crew_log_emit, "on_work_recorded", lambda unit, data: True)
    yield
    _SLOTS.clear()


def _req(method: str, path: str, *, body: Any, sk: str) -> web.Request:
    app = web.Application()
    state = MagicMock()
    state.get_slot = MagicMock(side_effect=lambda key: _SLOTS.get(key))
    app["state"] = state
    req = make_mocked_request(method, path, app=app, headers={"X-Session-Key": sk})
    req["internal_auth"] = True
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


async def _bound_item(conductor: str = CONDUCTOR) -> str:
    """goal, create, bind -- through the routes, so the bind is the real call site."""
    status, body = await _record(conductor, {"action": "goal", "goal": "ship it", "round": 1})
    assert status == 200, body
    status, body = await _record(
        conductor, {"action": "create", "title": "item one", "acceptance": ACCEPTANCE}
    )
    assert status == 200, body
    item_id = body["item"]["item_id"]
    _SLOTS[WORKER] = _Slot(created_by=conductor)
    status, body = await _record(
        conductor, {"action": "bind", "item_id": item_id, "worker_session_key": WORKER}
    )
    assert status == 200, body
    return item_id


@pytest.mark.asyncio
async def test_a_bind_through_the_route_records_the_dispatch(open_routes):
    item_id = await _bound_item()
    entries = _crew_entries()
    assert [entry["type"] for entry in entries] == [emit.CREW_DISPATCH]
    assert entries[0]["src"] == f"crew:{CREW}"
    assert entries[0]["data"] == {
        "item": item_id,
        "target": {"kind": "session", "slot": WORKER},
    }


@pytest.mark.asyncio
async def test_only_the_bind_action_records_a_dispatch(open_routes):
    """``goal`` and ``create`` are not dispatches: neither names a target."""
    status, body = await _record(CONDUCTOR, {"action": "goal", "goal": "ship it", "round": 1})
    assert status == 200, body
    status, body = await _record(
        CONDUCTOR, {"action": "create", "title": "item one", "acceptance": ACCEPTANCE}
    )
    assert status == 200, body
    assert not _crew_dir().exists()


@pytest.mark.asyncio
async def test_a_worker_report_through_the_route_records_the_answer(open_routes):
    _seed_session(entries=4)
    item_id = await _bound_item()
    status, body = await _report(WORKER, {"status": "done", "summary": "shipped it"})
    assert status == 200, body
    entries = _crew_entries()
    assert [entry["type"] for entry in entries] == [emit.CREW_DISPATCH, emit.CREW_REPORT]
    report = entries[1]
    assert report["data"] == {"item": item_id, "status": "done", "summary": "shipped it"}
    # Threaded onto the dispatch, and citing the worker's own unit as evidence.
    assert report["thread"] == entries[0]["seq"]
    assert report["ref"] == {"unit": lg.KIND_SESSION, "id": WORKER_UNIT, "from": 1, "to": 4}


@pytest.mark.asyncio
async def test_a_question_report_keeps_its_own_status(open_routes):
    """The work board's fourth status reaches the crew log rather than being folded.

    ``question`` and ``blocked`` differ by which party has to act, so collapsing
    them would erase the distinction a conductor reads the fold for.
    """
    _seed_session()
    await _bound_item()
    status, body = await _report(WORKER, {"status": "question", "summary": "which bar"})
    assert status == 200, body
    assert _crew_entries()[1]["data"]["status"] == "question"


@pytest.mark.asyncio
async def test_a_board_on_an_ordinary_chat_slot_records_no_crew_entry(open_routes):
    """A slot that names no crew has no crew log to own the record.

    The board itself is unaffected: its authority is the ``work/recorded`` entry in
    the acting session's log, which the route already refuses to proceed without.
    """
    chat_conductor = "chat-9-conductor"
    _seed_session(entries=2)
    item_id = await _bound_item(chat_conductor)
    status, body = await _report(WORKER, {"status": "done", "summary": "shipped it"})
    assert status == 200, body
    assert item_id
    assert not lg.crew_log_dir(lg.KIND_CREW, CREW).exists()
    assert routes._crew_store(chat_conductor) == ""


def test_the_crew_store_is_the_member_slug_and_nothing_else():
    assert routes._crew_store(CONDUCTOR) == CREW
    assert routes._crew_store(f"dashboard_{CONDUCTOR}") == CREW
    assert routes._crew_store("chat-9-conductor") == ""
    assert routes._crew_store("") == ""
