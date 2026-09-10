"""Conductor work ledger routes — Phase 2 exit criteria, one test per criterion.

Pins what the conductor-work-ledger RFC (``docs/request-for-change/rfc-conductor-work-ledger.md``,
revision v2) §Migration plan Phase 2 lists for the four tools and their routes: a
worker's report reaches its own item and no other, asserted against a
two-conductor two-worker fixture; every error code carries its tabulated HTTP
status; ``accept_batch`` parses in the real ``accept_eval.py`` and ignores a
worker's claimed ``pr``; a round trip through ``work_report`` writes no
conductor-owned field, asserted field by field; and each of the four caller states
in the dispatch table resolves as tabulated against both tool halves.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew import validation
from kiro_crew import work_ledger as wl
from kiro_crew.dashboard.handlers import work_ledger as routes

#: The real implementation, captured before the autouse fixture stubs the name.
#: A test that drives the true branch has to reach past its own default.
_REAL_REACHES_A_CHANNEL = routes._reaches_a_channel

CONDUCTOR_A = "chat-a-conductor"
CONDUCTOR_B = "chat-b-conductor"
WORKER_A = "chat-a-worker"
WORKER_B = "chat-b-worker"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Every test writes into its own data home, never the live one."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    yield


@pytest.fixture(autouse=True)
def _open_route(monkeypatch):
    """Bypass session recognition/restriction (their own suites cover them).

    Several tests below re-patch these to assert the refusals still fire, so the
    bypass is a default rather than an assumption.

    The channel-mirror probe is stubbed to "not mirrored" for the same reason and
    with the same caveat: the real probe reads the session store, which a
    ``make_mocked_request`` slot cannot answer, and it FAILS CLOSED — so leaving it
    live would make every test's caller read as mirrored and refused.
    ``test_an_outbound_mirrored_session_is_refused`` drives the true branch.
    """

    async def _recognized(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(routes, "_recognize_session", _recognized)
    monkeypatch.setattr(routes, "_is_restricted_session", lambda *a: False)
    monkeypatch.setattr(routes, "_reaches_a_channel", lambda request, sk: False)


class _Slot:
    """The two slot attributes these routes read, and nothing else.

    ``_created_by`` is what ``session_control.create_session`` stamps with the
    calling session's key — the attribution a bind's ownership check rests on — and
    ``workspace`` is the memory boundary it must not cross.
    """

    def __init__(
        self, created_by: str = "", workspace: str = "default", running: bool = False
    ) -> None:
        self._created_by = created_by
        self.workspace = workspace
        # ``_ChatSlot.running`` is a property over its asyncio task; a bool is the
        # whole of what these routes read from it.
        self.running = running


#: Slot table for the request under test, keyed exactly as the routes look keys
#: up. Reset per test by the autouse fixture, because a leaked worker slot would
#: make a later test's ``stale`` assertion pass for the wrong reason.
_SLOTS: dict[str, _Slot] = {}


@pytest.fixture(autouse=True)
def _clean_slots():
    _SLOTS.clear()
    yield
    _SLOTS.clear()


def _dispatched(
    worker: str, conductor: str, *, workspace: str = "default", running: bool = False
) -> None:
    """Register *worker* as a session *conductor* created, as session_create would."""
    _SLOTS[worker] = _Slot(created_by=conductor, workspace=workspace, running=running)


def _req(method: str, path: str, *, body: Any = ..., sk: str) -> web.Request:
    app = web.Application()
    state = MagicMock()
    # A real-shaped slot table rather than a MagicMock: a mock would answer every
    # ``get_slot`` with a truthy object, making every item look alive and every
    # bind look owned — the two things these routes decide from.
    state.get_slot = MagicMock(side_effect=lambda key: _SLOTS.get(key))
    app["state"] = state
    req = make_mocked_request(method, path, app=app, headers={"X-Session-Key": sk})
    # The routes require the internal-secret principal, positively confirmed — on
    # loopback a strict internal path with no secret header falls through to cookie
    # auth and is granted, so the handler cannot infer it from the path. This is
    # what ``token_auth`` sets for a verified secret caller.
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


async def _read(sk: str) -> tuple[int, dict[str, Any]]:
    resp = await routes.api_work_ledger_get(_req("GET", "/api/work-ledger", sk=sk))
    return resp.status, json.loads(resp.text)


async def _brief(sk: str) -> tuple[int, dict[str, Any]]:
    resp = await routes.api_work_brief(_req("GET", "/api/work-ledger/brief", sk=sk))
    return resp.status, json.loads(resp.text)


async def _dispatch(conductor: str, worker: str, title: str, acceptance: dict) -> str:
    """The conductor's own create-then-bind sequence, through the routes only."""
    status, body = await _record(
        conductor, {"action": "goal", "goal": f"goal for {title}", "round": 1}
    )
    assert status == 200, body
    status, body = await _record(
        conductor, {"action": "create", "title": title, "acceptance": acceptance}
    )
    assert status == 200, body
    item_id = body["item"]["item_id"]
    # The conductor dispatches the session BEFORE binding it, which is the order
    # the RFC's binding lifecycle specifies and the order the ownership check on
    # ``bind`` requires: the slot has to exist and be attributed to this conductor.
    _dispatched(worker, conductor)
    status, body = await _record(
        conductor, {"action": "bind", "item_id": item_id, "worker_session_key": worker}
    )
    assert status == 200, body
    return item_id


async def two_by_two() -> dict[str, str]:
    """Two conductors, one bound worker each — the fixture the RFC names.

    An awaited helper rather than a pytest fixture: an async fixture needs a
    plugin-provided decorator, and every consumer here is already a coroutine.
    """
    item_a = await _dispatch(CONDUCTOR_A, WORKER_A, "item A", {"kind": "human_approval"})
    item_b = await _dispatch(CONDUCTOR_B, WORKER_B, "item B", {"kind": "human_approval"})
    return {"item_a": item_a, "item_b": item_b}


# ── the isolation the whole design exists for ─────────────────────────────


@pytest.mark.asyncio
async def test_a_workers_report_reaches_its_own_item_and_no_other():
    """Worker A's report lands on A's item; B's item is byte-identical after it."""
    ids = await two_by_two()
    before = wl.item_path(CONDUCTOR_B, ids["item_b"]).read_bytes()

    status, _ = await _report(WORKER_A, {"status": "progress", "summary": "A moving"})
    assert status == 200

    item_a = wl.read_work_item(CONDUCTOR_A, ids["item_a"])
    item_b = wl.read_work_item(CONDUCTOR_B, ids["item_b"])
    assert item_a is not None and item_b is not None
    assert item_a.summary == "A moving"
    assert item_b.summary == ""
    assert item_b.status is None
    assert wl.item_path(CONDUCTOR_B, ids["item_b"]).read_bytes() == before


@pytest.mark.asyncio
async def test_a_worker_cannot_name_another_item_because_there_is_no_parameter():
    """The bound item is resolved, not supplied — naming one is an unknown field."""
    ids = await two_by_two()
    status, body = await _report(
        WORKER_A,
        {"status": "done", "summary": "mine now", "item_id": ids["item_b"]},
    )
    assert status == 400
    assert body["code"] == wl.CODE_INVALID_VALUE
    item_b = wl.read_work_item(CONDUCTOR_B, ids["item_b"])
    assert item_b is not None and item_b.status is None


@pytest.mark.asyncio
async def test_a_report_writes_no_conductor_owned_field():
    """Field by field: every conductor-owned value survives a worker round trip.

    Asserted as a set difference rather than as a list of five names, so a field
    added to the item record joins this check automatically instead of silently
    escaping it.
    """
    ids = await two_by_two()
    worker_owned = {"status", "summary", "artifacts", "pr", "last_report_at"}
    before = (wl.read_work_item(CONDUCTOR_A, ids["item_a"]) or wl.WorkItem()).to_dict()

    status, _ = await _report(
        WORKER_A,
        {
            "status": "done",
            "summary": "built it",
            "artifacts": {"pr": "42", "branch": "feat/x"},
            "pr": 42,
        },
    )
    assert status == 200

    after = (wl.read_work_item(CONDUCTOR_A, ids["item_a"]) or wl.WorkItem()).to_dict()
    changed = {k for k in after if before.get(k) != after.get(k)}
    assert changed <= worker_owned, f"a worker changed conductor-owned field(s): {changed}"
    # And the conductor's own values are still exactly what it wrote.
    assert after["state"] == "open"
    assert after["verdict"] is None
    assert after["acceptance"] == {"kind": "human_approval"}
    assert after["title"] == "item A"


@pytest.mark.asyncio
async def test_a_brief_shows_one_item_and_never_a_sibling_or_the_goal():
    ids = await two_by_two()
    status, body = await _brief(WORKER_A)
    assert status == 200
    brief = body["brief"]
    assert brief["item_id"] == ids["item_a"]
    assert brief["title"] == "item A"
    assert "goal" not in brief
    assert "items" not in brief
    assert ids["item_b"] not in json.dumps(brief)


# ── the dispatch table, cell by cell ──────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_table_binding_only_is_a_worker():
    """A binding file and no ledger directory: worker half answers, conductor 404s."""
    await two_by_two()
    assert (await _brief(WORKER_A))[0] == 200
    assert (await _report(WORKER_A, {"status": "progress", "summary": "x"}))[0] == 200
    status, body = await _read(WORKER_A)
    assert status == 404
    assert body["code"] == wl.CODE_NO_LEDGER
    status, body = await _record(
        WORKER_A, {"action": "verdict", "item_id": "it_00000000", "verdict": "pass"}
    )
    assert status == 404
    assert body["code"] == wl.CODE_NO_LEDGER


@pytest.mark.asyncio
async def test_dispatch_table_ledger_only_is_a_conductor():
    """A ledger directory and no binding: conductor half answers, worker 403s."""
    await two_by_two()
    assert (await _read(CONDUCTOR_A))[0] == 200
    status, body = await _brief(CONDUCTOR_A)
    assert status == 403
    assert body["code"] == routes.CODE_NOT_BOUND
    status, body = await _report(CONDUCTOR_A, {"status": "done", "summary": "x"})
    assert status == 403
    assert body["code"] == routes.CODE_NOT_BOUND


@pytest.mark.asyncio
async def test_dispatch_table_both_is_a_second_level_conductor():
    """Worker A opens its own ledger: all four answer, and depth is one past A's."""
    ids = await two_by_two()
    status, body = await _record(WORKER_A, {"action": "goal", "goal": "sub-goal", "round": 1})
    assert status == 200, body
    assert body["conductor"]["depth"] == 1
    assert body["conductor"]["parent_item"] == ids["item_a"]

    assert (await _brief(WORKER_A))[0] == 200
    assert (await _report(WORKER_A, {"status": "progress", "summary": "sub"}))[0] == 200
    assert (await _read(WORKER_A))[0] == 200
    status, _ = await _record(
        WORKER_A,
        {"action": "create", "title": "sub item", "acceptance": {"kind": "human_approval"}},
    )
    assert status == 200


@pytest.mark.asyncio
async def test_dispatch_table_neither_gets_both_refusals():
    status, body = await _brief("chat-nobody")
    assert status == 403
    assert body["code"] == routes.CODE_NOT_BOUND
    status, body = await _report("chat-nobody", {"status": "done", "summary": "x"})
    assert status == 403
    assert body["code"] == routes.CODE_NOT_BOUND
    status, body = await _read("chat-nobody")
    assert status == 404
    assert body["code"] == wl.CODE_NO_LEDGER
    status, body = await _record(
        "chat-nobody", {"action": "decide", "item_id": "it_00000000", "decision": "x"}
    )
    assert status == 404
    assert body["code"] == wl.CODE_NO_LEDGER


@pytest.mark.asyncio
async def test_a_third_level_conductor_is_refused_at_the_depth_cap():
    """depth <= 2: a grandchild may work, and may not conduct."""
    await two_by_two()
    assert (await _record(WORKER_A, {"action": "goal", "goal": "level 1", "round": 1}))[0] == 200
    grandchild = "chat-a-grandchild"
    _dispatched(grandchild, WORKER_A)
    status, body = await _record(
        WORKER_A, {"action": "create", "title": "leaf", "acceptance": {"kind": "human_approval"}}
    )
    assert status == 200
    leaf = body["item"]["item_id"]
    assert (
        await _record(
            WORKER_A, {"action": "bind", "item_id": leaf, "worker_session_key": grandchild}
        )
    )[0] == 200
    # The grandchild is a working worker...
    assert (await _report(grandchild, {"status": "progress", "summary": "leaf work"}))[0] == 200
    # ...and its ledger opens at the cap (depth 2 == MAX_DEPTH) ...
    status, body = await _record(grandchild, {"action": "goal", "goal": "level 2", "round": 1})
    assert status == 200, body
    assert body["conductor"]["depth"] == wl.MAX_DEPTH
    # ...where it can dispatch nothing, which is what the cap buys: a summary of
    # summaries of summaries is not evidence any more.
    status, body = await _record(
        grandchild,
        {"action": "create", "title": "great-grandchild", "acceptance": {"kind": "human_approval"}},
    )
    assert status == 409
    assert body["code"] == wl.CODE_DEPTH_EXCEEDED


# ── every error code carries its tabulated status ─────────────────────────


@pytest.mark.asyncio
async def test_every_store_code_maps_to_the_status_the_rfc_tabulates():
    """The map is exhaustive over the store's ``CODE_*`` constants.

    Without this, a code the store gains later degrades to 400 unnoticed, and a
    409-shaped conflict would reach the model as a bad-argument error.
    """
    store_codes = {
        value
        for name, value in vars(wl).items()
        if name.startswith("CODE_") and isinstance(value, str)
    }
    assert store_codes == set(routes._CODE_STATUS), (
        "the code->status map and the store's CODE_* constants disagree: "
        f"{store_codes ^ set(routes._CODE_STATUS)}"
    )
    assert routes._CODE_STATUS == {
        "no_ledger": 404,
        "unknown_item": 404,
        "already_bound": 409,
        "item_closed": 409,
        "item_cap_exceeded": 409,
        "depth_exceeded": 409,
        "field_too_long": 400,
        "invalid_action": 400,
        "invalid_status": 400,
        "invalid_value": 400,
    }


@pytest.mark.asyncio
async def test_unknown_item_is_404():
    await two_by_two()
    status, body = await _record(
        CONDUCTOR_A, {"action": "decide", "item_id": "it_deadbeef", "decision": "nope"}
    )
    assert status == 404
    assert body["code"] == wl.CODE_UNKNOWN_ITEM


@pytest.mark.asyncio
async def test_already_bound_is_409():
    ids = await two_by_two()
    _dispatched("chat-other", CONDUCTOR_A)
    status, body = await _record(
        CONDUCTOR_A,
        {"action": "bind", "item_id": ids["item_a"], "worker_session_key": "chat-other"},
    )
    assert status == 409
    assert body["code"] == wl.CODE_ALREADY_BOUND


@pytest.mark.asyncio
async def test_item_closed_is_409_for_both_halves():
    ids = await two_by_two()
    status, _ = await _record(
        CONDUCTOR_A, {"action": "close", "item_id": ids["item_a"], "state": "accepted"}
    )
    assert status == 200
    status, body = await _report(WORKER_A, {"status": "done", "summary": "too late"})
    assert status == 409
    assert body["code"] == wl.CODE_ITEM_CLOSED
    status, body = await _record(
        CONDUCTOR_A, {"action": "decide", "item_id": ids["item_a"], "decision": "late"}
    )
    assert status == 409
    assert body["code"] == wl.CODE_ITEM_CLOSED


@pytest.mark.asyncio
async def test_item_cap_exceeded_is_409():
    await two_by_two()
    for n in range(wl.MAX_ITEMS_PER_CONDUCTOR - 1):
        status, _ = await _record(
            CONDUCTOR_A,
            {"action": "create", "title": f"item {n}", "acceptance": {"kind": "human_approval"}},
        )
        assert status == 200
    status, body = await _record(
        CONDUCTOR_A,
        {"action": "create", "title": "one too many", "acceptance": {"kind": "human_approval"}},
    )
    assert status == 409
    assert body["code"] == wl.CODE_ITEM_CAP_EXCEEDED


@pytest.mark.asyncio
async def test_field_too_long_is_400_and_names_the_field():
    await two_by_two()
    status, body = await _report(
        WORKER_A, {"status": "progress", "summary": "x" * (wl.MAX_SUMMARY_CHARS + 1)}
    )
    assert status == 400
    assert body["code"] == wl.CODE_FIELD_TOO_LONG
    assert "summary" in body["error"]


@pytest.mark.asyncio
async def test_invalid_status_is_400():
    await two_by_two()
    status, body = await _report(WORKER_A, {"status": "finished", "summary": "x"})
    assert status == 400
    assert body["code"] == wl.CODE_INVALID_STATUS


@pytest.mark.asyncio
async def test_invalid_action_is_400():
    await two_by_two()
    status, body = await _record(CONDUCTOR_A, {"action": "delete"})
    assert status == 400
    assert body["code"] == wl.CODE_INVALID_ACTION


@pytest.mark.asyncio
async def test_an_unrecognized_session_is_refused_before_the_store(monkeypatch):
    async def _refused(*a: Any, **k: Any) -> web.Response:
        return web.json_response(
            {"error": "unknown session", "code": "unknown_session"}, status=403
        )

    monkeypatch.setattr(routes, "_recognize_session", _refused)
    for call in (
        routes.api_work_brief(_req("GET", "/api/work-ledger/brief", sk="chat-x")),
        routes.api_work_ledger_get(_req("GET", "/api/work-ledger", sk="chat-x")),
    ):
        assert (await call).status == 403


@pytest.mark.asyncio
async def test_a_restricted_session_is_refused(monkeypatch):
    await two_by_two()
    monkeypatch.setattr(routes, "_is_restricted_session", lambda *a: True)
    resp = await routes.api_work_report(
        _req(
            "POST", "/api/work-ledger/report", body={"status": "done", "summary": "x"}, sk=WORKER_A
        )
    )
    assert resp.status == 403
    assert json.loads(resp.text)["code"] == "restricted_session"


# ── acceptance stays the conductor's ──────────────────────────────────────


@pytest.mark.asyncio
async def test_accept_batch_parses_in_the_real_accept_eval():
    """Piped into the bundled script, unmodified, and read back as its verdicts."""
    ids = await two_by_two()
    script = (
        Path(__file__).resolve().parents[1]
        / "src/kiro_crew/builtin_skills/goal-conductor/scripts/accept_eval.py"
    )
    assert script.is_file(), script
    _, body = await _read(CONDUCTOR_A)
    batch = body["accept_batch"]
    assert batch["items"], batch
    proc = subprocess.run(
        [sys.executable, str(script)],
        input=json.dumps(batch).encode(),
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr.decode()
    parsed = json.loads(proc.stdout.decode())
    # ``accept_eval.py`` answers ``{"results": [{"id", "verdict", "evidence"}]}`` —
    # one verdict per item it was handed, which is what "the batch parses" means.
    evaluated = {row.get("id") for row in parsed["results"]}
    assert ids["item_a"] in evaluated, parsed
    verdicts = {row["verdict"] for row in parsed["results"]}
    assert verdicts <= wl.VERDICTS, verdicts


@pytest.mark.asyncio
async def test_accept_batch_ignores_a_worker_supplied_pr():
    """The claim is surfaced beside the item and never enters the bar."""
    ids = await two_by_two()
    status, _ = await _report(WORKER_A, {"status": "done", "summary": "green", "pr": 999_111})
    assert status == 200
    _, body = await _read(CONDUCTOR_A)
    row = next(r for r in body["items"] if r["item_id"] == ids["item_a"])
    assert row["pr"] == 999_111, "the claim must be visible to the conductor"
    batch_text = json.dumps(body["accept_batch"])
    assert "999111" not in batch_text
    for entry in body["accept_batch"]["items"]:
        assert "pr" not in entry["accept"], entry


@pytest.mark.asyncio
async def test_the_conductor_promotes_a_claim_with_the_accept_action():
    """``accept`` is the explicit write that moves the bar — and only it does."""
    await two_by_two()
    item_id = await _dispatch(
        CONDUCTOR_A, "chat-a-worker-2", "pending pr", {"kind": "pr_checks", "pr": "TBD"}
    )
    status, _ = await _report(
        "chat-a-worker-2", {"status": "done", "summary": "opened it", "pr": 4321}
    )
    assert status == 200
    _, body = await _read(CONDUCTOR_A)
    # Absent from the batch while the bar still says "TBD", and the item row says why.
    assert not [e for e in body["accept_batch"]["items"] if e["id"] == item_id]
    row = next(r for r in body["items"] if r["item_id"] == item_id)
    assert row["acceptance_concrete"] is False
    assert row["acceptance"]["pr"] == "TBD", "the stored bar is untouched, just not batched"

    status, body = await _record(
        CONDUCTOR_A,
        {
            "action": "accept",
            "item_id": item_id,
            "acceptance": {"kind": "pr_checks", "pr": 4321, "repo": "kirodotdev/KiroCrew"},
        },
    )
    assert status == 200, body
    _, body = await _read(CONDUCTOR_A)
    entry = next(e for e in body["accept_batch"]["items"] if e["id"] == item_id)
    assert entry["accept"]["pr"] == 4321
    # One event per write, and the promotion is one of them.
    row = next(r for r in body["items"] if r["item_id"] == item_id)
    assert row["acceptance_concrete"] is True
    assert any(e["kind"] == "decision" for e in row["events"])


@pytest.mark.asyncio
async def test_a_read_says_per_item_why_an_item_is_not_in_the_batch():
    """``acceptance_concrete`` is derived on the row, not stored, so a conductor can
    tell "the bar is not filled in yet" from "the read dropped my item"."""
    ids = await two_by_two()
    pending = await _dispatch(
        CONDUCTOR_A, "chat-a-worker-3", "no number yet", {"kind": "pr_checks", "pr": "TBD"}
    )
    _, body = await _read(CONDUCTOR_A)
    rows = {r["item_id"]: r for r in body["items"]}
    assert rows[pending]["acceptance_concrete"] is False
    assert rows[ids["item_a"]]["acceptance_concrete"] is True
    batched = {e["id"] for e in body["accept_batch"]["items"]}
    assert pending not in batched
    assert ids["item_a"] in batched
    stored = json.loads(wl.item_path(CONDUCTOR_A, pending).read_text())
    assert "acceptance_concrete" not in stored


@pytest.mark.asyncio
async def test_each_batch_entry_carries_the_items_status_unfiltered():
    """The conductor filters to ``done``; the read hands it the field to filter on and
    filters nothing itself."""
    ids = await two_by_two()
    status, _ = await _report(WORKER_A, {"status": "progress", "summary": "moving"})
    assert status == 200
    _, body = await _read(CONDUCTOR_A)
    entry = next(e for e in body["accept_batch"]["items"] if e["id"] == ids["item_a"])
    assert entry["status"] == "progress"
    assert entry["accept"] == {"kind": "human_approval"}

    status, _ = await _report(WORKER_A, {"status": "done", "summary": "met"})
    assert status == 200
    _, body = await _read(CONDUCTOR_A)
    entry = next(e for e in body["accept_batch"]["items"] if e["id"] == ids["item_a"])
    assert entry["status"] == "done"


@pytest.mark.asyncio
async def test_a_done_item_waiting_on_the_conductor_is_not_stale():
    """``stale`` means the WORKER went quiet. Once it has claimed its bar is met the
    next move is the conductor's or a human's, so the flag must not point back at the
    reader — a quiet ``done`` item is silent because it is finished."""
    ids = await two_by_two()
    status, _ = await _report(WORKER_A, {"status": "done", "summary": "opened the pr"})
    assert status == 200
    item = wl.read_work_item(CONDUCTOR_A, ids["item_a"])
    assert item is not None
    long_after = datetime.now().astimezone() + timedelta(seconds=wl.DEFAULT_STALE_WINDOW_SECS * 6)
    assert (
        routes._slot_running(_req("GET", "/api/work-ledger", sk=CONDUCTOR_A).app["state"], WORKER_A)
        is False
    )
    assert wl.is_stale(item, worker_running=False, now=long_after) is False
    _, body = await _read(CONDUCTOR_A)
    row = next(r for r in body["items"] if r["item_id"] == ids["item_a"])
    assert row["stale"] is False


@pytest.mark.asyncio
async def test_accept_is_a_route_action_not_a_store_action():
    """The store's six are pinned by Phase 1, so the seventh lives one layer up."""
    assert "accept" not in wl.CONDUCTOR_ACTIONS
    assert "accept" in routes.RECORD_ACTIONS
    assert routes.RECORD_ACTIONS == wl.CONDUCTOR_ACTIONS | {"accept"}
    assert routes.RECORD_ACTIONS == validation._WORK_RECORD_ACTIONS


# ── derived flags, and the schema caps that restate the store's ───────────


@pytest.mark.asyncio
async def test_orphaned_and_stale_are_derived_not_stored():
    """Neither flag is a field, and both come back on a read."""
    ids = await two_by_two()
    _, body = await _read(CONDUCTOR_A)
    row = next(r for r in body["items"] if r["item_id"] == ids["item_a"])
    assert row["orphaned"] is True  # the mocked slot table reports nothing open
    # NOT stale: just created, so it is inside the window even with no worker
    # running. The window exists to cover the gap between bind and the first
    # report, not to flag an item the moment it is dispatched.
    assert row["stale"] is False
    stored = json.loads(wl.item_path(CONDUCTOR_A, ids["item_a"]).read_text())
    assert "orphaned" not in stored
    assert "stale" not in stored


@pytest.mark.asyncio
async def test_an_item_past_the_window_with_no_running_worker_is_stale():
    """Both conditions, and only both: the conjunction is what the flag means."""
    ids = await two_by_two()
    item = wl.read_work_item(CONDUCTOR_A, ids["item_a"])
    assert item is not None
    aged = datetime.now().astimezone() - timedelta(seconds=wl.DEFAULT_STALE_WINDOW_SECS + 60)
    assert wl.is_stale(item, worker_running=False, now=aged + timedelta(days=1)) is True
    assert wl.is_stale(item, worker_running=True, now=aged + timedelta(days=1)) is False


@pytest.mark.asyncio
async def test_an_idle_open_worker_past_the_window_is_stale():
    """ "Running" means a turn in flight, not an open tab. A worker whose session
    is still open but which stopped without reporting is the case the flag exists
    for, so slot EXISTENCE must not satisfy it."""
    ids = await two_by_two()
    item = wl.read_work_item(CONDUCTOR_A, ids["item_a"])
    assert item is not None
    aged = datetime.now().astimezone() + timedelta(seconds=wl.DEFAULT_STALE_WINDOW_SECS + 60)
    request = _req("GET", "/api/work-ledger", sk=CONDUCTOR_A)
    # The worker slot exists (dispatched by the fixture) but has no turn running.
    assert routes._slot_open(request.app["state"], WORKER_A) is True
    assert routes._slot_running(request.app["state"], WORKER_A) is False
    assert wl.is_stale(item, worker_running=False, now=aged) is True
    # And with a turn in flight the same item is not stale.
    _dispatched(WORKER_A, CONDUCTOR_A, running=True)
    assert routes._slot_running(request.app["state"], WORKER_A) is True
    assert wl.is_stale(item, worker_running=True, now=aged) is False


@pytest.mark.asyncio
async def test_a_running_worker_is_never_stale(monkeypatch):
    """The conjunction is the point: silence alone does not flag an item."""
    ids = await two_by_two()
    monkeypatch.setattr(routes, "_slot_open", lambda state, key: bool(key))
    monkeypatch.setattr(routes, "_slot_running", lambda state, key: bool(key))
    _, body = await _read(CONDUCTOR_A)
    row = next(r for r in body["items"] if r["item_id"] == ids["item_a"])
    assert row["stale"] is False
    assert row["orphaned"] is False


def test_the_schema_caps_restate_the_stores_own():
    """``validation`` cannot import the store on the gateway's request path, so the
    two spell the same numbers — and drifting apart would let a value through the
    schema that the store then refuses with a different code."""
    report = validation.WORK_REPORT_SCHEMA
    summary = next(f for f in report.fields if f.name == "summary")
    assert summary.max_len == wl.MAX_SUMMARY_CHARS
    pr = next(f for f in report.fields if f.name == "pr")
    assert (pr.min_val, pr.max_val) == (wl.MIN_PR, wl.MAX_PR)
    status = next(f for f in report.fields if f.name == "status")
    assert status.allowed == wl.WORKER_STATUSES

    record = validation.WORK_LEDGER_RECORD_SCHEMA
    assert next(f for f in record.fields if f.name == "title").max_len == wl.MAX_TITLE_CHARS
    assert next(f for f in record.fields if f.name == "goal").max_len == wl.MAX_GOAL_CHARS
    assert next(f for f in record.fields if f.name == "decision").max_len == wl.MAX_DECISION_CHARS
    assert next(f for f in record.fields if f.name == "verdict").allowed == wl.VERDICTS
    assert next(f for f in record.fields if f.name == "state").allowed <= wl.ITEM_STATES


def test_the_worker_schema_has_no_conductor_field():
    """The absence IS the guarantee — stronger than an allowlist kept correct by hand."""
    names = {f.name for f in validation.WORK_REPORT_SCHEMA.fields}
    assert names == {"status", "summary", "artifacts", "pr"}
    for forbidden in (
        "item_id",
        "session",
        "session_key",
        "acceptance",
        "verdict",
        "state",
        "decision",
        "title",
        "goal",
        "round",
        "fails",
        "worker_session_key",
    ):
        assert forbidden not in names, forbidden


@pytest.mark.asyncio
async def test_an_oversized_artifacts_map_is_refused_not_truncated():
    ids = await two_by_two()
    status, body = await _report(
        WORKER_A,
        {
            "status": "progress",
            "summary": "x",
            "artifacts": {f"k{n}": "v" for n in range(wl.MAX_ARTIFACT_KEYS + 1)},
        },
    )
    assert status == 400
    assert body["code"] == wl.CODE_FIELD_TOO_LONG
    item = wl.read_work_item(CONDUCTOR_A, ids["item_a"])
    assert item is not None and item.artifacts == {}


# ── the three refusals this layer owns, above the store ──────────────────


@pytest.mark.asyncio
async def test_bind_refuses_a_worker_this_conductor_did_not_create():
    """Otherwise conductor A binds B's idle worker, and B's worker then reads A's
    brief and A's ``decision`` — the one field a worker treats as an instruction.
    The store cannot see this: it checks only that the item is unbound."""
    ids = await two_by_two()
    victim = "chat-b-worker-2"
    _dispatched(victim, CONDUCTOR_B)  # created by the OTHER conductor
    status, body = await _record(
        CONDUCTOR_A,
        {"action": "create", "title": "hijack", "acceptance": {"kind": "human_approval"}},
    )
    assert status == 200
    item_id = body["item"]["item_id"]
    status, body = await _record(
        CONDUCTOR_A,
        {"action": "bind", "item_id": item_id, "worker_session_key": victim},
    )
    assert status == 403
    assert body["code"] == "worker_not_owned"
    # And the victim is still bound to nothing of A's.
    assert wl.read_binding(victim) is None
    item = wl.read_work_item(CONDUCTOR_A, item_id)
    assert item is not None and item.worker_session_key is None
    assert ids  # the two-by-two fixture stands untouched


@pytest.mark.asyncio
async def test_bind_refuses_a_session_that_is_not_open():
    ids = await two_by_two()
    status, body = await _record(
        CONDUCTOR_A,
        {"action": "create", "title": "no session", "acceptance": {"kind": "human_approval"}},
    )
    assert status == 200
    status, body = await _record(
        CONDUCTOR_A,
        {
            "action": "bind",
            "item_id": body["item"]["item_id"],
            "worker_session_key": "chat-never-created",
        },
    )
    assert status == 404
    assert body["code"] == "unknown_worker_session"
    assert ids


@pytest.mark.asyncio
async def test_bind_refuses_across_a_workspace_boundary():
    """Workspace is the memory boundary ``authorize_target`` already refuses across."""
    ids = await two_by_two()
    _SLOTS[CONDUCTOR_A] = _Slot(workspace="alpha")
    _dispatched("chat-a-worker-elsewhere", CONDUCTOR_A, workspace="beta")
    status, body = await _record(
        CONDUCTOR_A,
        {"action": "create", "title": "elsewhere", "acceptance": {"kind": "human_approval"}},
    )
    assert status == 200
    status, body = await _record(
        CONDUCTOR_A,
        {
            "action": "bind",
            "item_id": body["item"]["item_id"],
            "worker_session_key": "chat-a-worker-elsewhere",
        },
    )
    assert status == 403
    assert body["code"] == "worker_cross_workspace"
    assert ids


@pytest.mark.asyncio
async def test_a_channel_session_is_refused_on_every_tool():
    """The channel-agent block in ``channel.py`` matches a rendered PERMISSION
    REQUEST, and the four tools are auto-approved on three specs — so an
    ``allowedTools`` grant emits no permission event and that block never runs.
    Containment therefore has to hold here, where no spec can route around it."""
    for sk in ("slack:C123:456.789", "discord:guild:chan:1", "telegram:99:1"):
        status, body = await _brief(sk)
        assert status == 403, sk
        assert body["code"] == "channel_session", sk
        assert (await _report(sk, {"status": "done", "summary": "x"}))[0] == 403
        assert (await _read(sk))[0] == 403
        assert (await _record(sk, {"action": "goal", "goal": "g", "round": 1}))[0] == 403


@pytest.mark.asyncio
async def test_an_outbound_mirrored_session_is_refused_like_a_channel_one(monkeypatch):
    """A dashboard-BORN session given an outbound mirror republishes every turn to a
    channel, so its key looks local while the disclosure is identical. The key alone
    is therefore not the test."""
    ids = await two_by_two()
    monkeypatch.setattr(routes, "_reaches_a_channel", lambda request, sk: True)
    for status, body in (
        await _brief(WORKER_A),
        await _report(WORKER_A, {"status": "done", "summary": "x"}),
        await _read(CONDUCTOR_A),
        await _record(CONDUCTOR_A, {"action": "goal", "goal": "g", "round": 1}),
    ):
        assert status == 403
        assert body["code"] == "channel_session"
    assert ids


def test_the_mirror_probe_fails_closed_on_an_unreadable_store(monkeypatch):
    """An unreadable link counts as mirrored rather than opening the boundary —
    the same direction ``session_control._has_channel_mirror`` defaults to."""
    from kiro_crew.dashboard import session_control

    _SLOTS["chat-probe"] = _Slot()

    def _boom(*a: Any, **k: Any) -> bool:
        raise RuntimeError("session store unreadable")

    monkeypatch.setattr(session_control, "_has_channel_mirror", _boom)
    request = _req("GET", "/api/work-ledger", sk="chat-probe")
    assert _REAL_REACHES_A_CHANNEL(request, "chat-probe") is True


@pytest.mark.asyncio
async def test_accept_refuses_to_clear_the_bar_when_acceptance_is_omitted():
    """Coercing a missing acceptance to ``{}`` replaced the real condition, dropped
    the item out of ``accept_batch`` (which filters on a non-empty acceptance), and
    answered 200 — conductor-owned state lost with no signal. A promotion never
    legitimately clears a bar."""
    ids = await two_by_two()
    before = wl.read_work_item(CONDUCTOR_A, ids["item_a"])
    assert before is not None and before.acceptance == {"kind": "human_approval"}

    for body in (
        {"action": "accept", "item_id": ids["item_a"]},
        {"action": "accept", "item_id": ids["item_a"], "acceptance": {}},
    ):
        status, payload = await _record(CONDUCTOR_A, body)
        assert status == 400, payload
        assert payload["code"] == wl.CODE_INVALID_VALUE
        assert payload["field"] == "acceptance"

    after = wl.read_work_item(CONDUCTOR_A, ids["item_a"])
    assert after is not None and after.acceptance == {"kind": "human_approval"}
    # And it is still in the batch the evaluator reads.
    _, read = await _read(CONDUCTOR_A)
    assert any(e["id"] == ids["item_a"] for e in read["accept_batch"]["items"])


@pytest.mark.asyncio
async def test_an_unreadable_parent_ledger_refuses_the_bootstrap_rather_than_resetting_depth():
    """``read_conductor`` answers None for an absent record AND a torn one. Treating
    that as 'no parent' would compute depth 1 for a child of a depth-1 parent and
    PERSIST it, granting a generation no later read corrects — a cap that fails open
    on unreadable input is not a cap."""
    ids = await two_by_two()
    assert (await _record(WORKER_A, {"action": "goal", "goal": "level 1", "round": 1}))[0] == 200
    grandchild = "chat-a-grandchild"
    _dispatched(grandchild, WORKER_A)
    status, body = await _record(
        WORKER_A, {"action": "create", "title": "leaf", "acceptance": {"kind": "human_approval"}}
    )
    assert status == 200
    assert (
        await _record(
            WORKER_A,
            {
                "action": "bind",
                "item_id": body["item"]["item_id"],
                "worker_session_key": grandchild,
            },
        )
    )[0] == 200

    # Truncate the parent's own record so it reads as absent.
    wl.conductor_dir(WORKER_A).joinpath("conductor.json").write_text("{ tor", encoding="utf-8")
    assert wl.read_conductor(WORKER_A) is None

    status, body = await _record(grandchild, {"action": "goal", "goal": "level 2", "round": 1})
    assert status == 409
    assert body["code"] == "parent_unreadable"
    # Nothing was persisted at the wrong depth.
    assert wl.read_conductor(grandchild) is None
    assert ids


@pytest.mark.asyncio
async def test_a_worker_session_is_bound_once_and_never_rebound():
    """The store permits replacing a binding whose item is terminal, so a session
    COULD be reused — but a report already in flight from the old work resolves its
    binding when it LANDS, and there is no unbind path to undo the overwrite. An idle
    check cannot close that: a turn can start during the awaits before the commit.
    So the route refuses reuse outright, which makes the race unrepresentable."""
    ids = await two_by_two()
    # Close A's item, so the STORE would now allow the binding to be replaced.
    status, _ = await _record(
        CONDUCTOR_A, {"action": "close", "item_id": ids["item_a"], "state": "accepted"}
    )
    assert status == 200
    status, body = await _record(
        CONDUCTOR_A,
        {"action": "create", "title": "second", "acceptance": {"kind": "human_approval"}},
    )
    assert status == 200
    second = body["item"]["item_id"]

    status, body = await _record(
        CONDUCTOR_A,
        {"action": "bind", "item_id": second, "worker_session_key": WORKER_A},
    )
    assert status == 409
    assert body["code"] == "worker_already_dispatched"
    item = wl.read_work_item(CONDUCTOR_A, second)
    assert item is not None and item.worker_session_key is None
    # The original binding is untouched, so no report can be misrouted.
    assert wl.read_binding(WORKER_A) == (CONDUCTOR_A, ids["item_a"])

    # A FRESH session for the new item is the supported path.
    _dispatched("chat-a-worker-2", CONDUCTOR_A)
    status, body = await _record(
        CONDUCTOR_A,
        {"action": "bind", "item_id": second, "worker_session_key": "chat-a-worker-2"},
    )
    assert status == 200, body


def test_the_binding_presence_check_fails_closed(monkeypatch):
    """An unreadable store must not admit a rebind: the alternative is deciding the
    one question the refusal exists for on a store that could not answer."""

    def _boom(_key: str):
        raise OSError("store unreadable")

    monkeypatch.setattr(routes.work_ledger, "binding_path", _boom)
    assert routes._has_binding("chat-whoever") is True


@pytest.mark.asyncio
async def test_a_read_refuses_when_a_mirror_appears_during_the_read(monkeypatch):
    """Containment decided on ENTRY says nothing about containment after an await.
    The same reason ``session_control`` applies ``_refuse_ineligible_creator``
    twice — once on entry, once immediately before it allocates."""
    ids = await two_by_two()
    calls = {"n": 0}

    def _mirror_appears_after_entry(request: Any, sk: str) -> bool:
        # False for the entry check, True for the post-read re-check.
        calls["n"] += 1
        return calls["n"] > 1

    monkeypatch.setattr(routes, "_reaches_a_channel", _mirror_appears_after_entry)
    status, body = await _brief(WORKER_A)
    assert status == 403
    assert body["code"] == "channel_session"
    assert "while the brief was being read" in body["error"]

    calls["n"] = 0
    status, body = await _read(CONDUCTOR_A)
    assert status == 403
    assert body["code"] == "channel_session"
    assert "while the ledger was being read" in body["error"]
    assert ids


def test_the_route_owned_codes_are_disjoint_from_the_stores():
    """``_CODE_STATUS`` is asserted exhaustive over the store's ``CODE_*``, so a
    route-only code must not be folded into it or that assertion goes hollow."""
    assert not (routes.ROUTE_CODES & set(routes._CODE_STATUS))
    assert routes.CODE_NOT_BOUND not in routes.ROUTE_CODES  # its own named constant
    for code in (
        "channel_session",
        "parent_unreadable",
        "unknown_worker_session",
        "worker_not_owned",
        "worker_cross_workspace",
        "worker_already_dispatched",
    ):
        assert code in routes.ROUTE_CODES, code


# ── the routes are reachable by the tools and by nothing else ─────────────


def test_the_store_directory_is_fenced_from_file_tools():
    """The routes are the only sanctioned path in. The worker agent carries the
    full default file toolset, so without both fences those auto-approved tools
    reach every conductor's records directly — and a corrupted record reads as
    ABSENT to the store, so the loss is silent. Same treatment as the sibling
    ``ledger/`` directory, for the same reason."""
    from kiro_crew import sandbox
    from kiro_crew.security import is_sensitive_path

    for prefix in (".kiro/crew", ".kirocrew"):
        assert is_sensitive_path(f"~/{prefix}/work-ledger") is True
        assert is_sensitive_path(f"~/{prefix}/work-ledger/bindings/x.json") is True
        assert is_sensitive_path(f"~/{prefix}/work-ledger/c-abc12345/items/it_0.json") is True
    assert "work-ledger" in sandbox._CREW_HIDDEN_LEAVES


def test_the_routes_are_on_the_strict_internal_allowlist():
    """The four tools authenticate with the internal secret; without this entry the
    call falls through to cookie auth and every one fails with 403 before the
    handler's own session recognition can run."""
    from kiro_crew.dashboard import server

    assert "/api/work-ledger" in server._STRICT_INTERNAL_API_PATHS


def test_every_route_is_registered_lazily_on_the_app():
    """Registered by path AND by method, since a GET-only registration of the two
    write routes would fail only at call time — and through the DEFERRED binder, so
    an opt-in subsystem's module is not imported on the gateway boot path.
    """
    import inspect

    from kiro_crew.dashboard import server

    src = inspect.getsource(server)
    for method, path, handler in (
        ("add_get", "/api/work-ledger", "api_work_ledger_get"),
        ("add_post", "/api/work-ledger/record", "api_work_ledger_record"),
        ("add_get", "/api/work-ledger/brief", "api_work_brief"),
        ("add_post", "/api/work-ledger/report", "api_work_report"),
    ):
        assert f'{method}("{path}", _deferred_work_ledger("{handler}"))' in src, (method, path)
        # And never eagerly, which is what the boot-path rule forbids.
        assert f'{method}("{path}", handlers.{handler})' not in src, (method, path)


def test_the_handler_package_does_not_import_the_subsystem_at_boot():
    """``handlers/__init__.py`` is reached from ``start_dashboard``, so an import
    there IS the boot path. The four handlers belong to an opt-in MCP server, and
    clause 5 of ``no-new-work-on-gateway-boot-path`` requires gating the IMPORT, not
    just the handler."""
    package_init = (
        Path(__file__).resolve().parents[1] / "src/kiro_crew/dashboard/handlers/__init__.py"
    )
    text = package_init.read_text(encoding="utf-8")
    assert "from kiro_crew.dashboard.handlers.work_ledger import" not in text
    for name in (
        "api_work_brief",
        "api_work_report",
        "api_work_ledger_get",
        "api_work_ledger_record",
    ):
        assert f"    {name},\n" not in text, name


def test_the_deferred_binder_resolves_each_handler():
    """The binder resolves by NAME, so a renamed handler would fail only at request
    time — this pins all four names against the module."""
    from kiro_crew.dashboard import server

    for name in (
        "api_work_brief",
        "api_work_report",
        "api_work_ledger_get",
        "api_work_ledger_record",
    ):
        bound = server._deferred_work_ledger(name)
        assert bound.__name__ == name
        assert getattr(routes, name, None) is not None, name
