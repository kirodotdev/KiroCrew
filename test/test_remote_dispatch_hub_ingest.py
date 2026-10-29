"""The hub half of remote dispatch, wave 4 (rfc-conductor-remote-dispatch §2, §3, §5).

Drives the real work-ledger routes and the real store with a stub crew carrier that
records every call the hub makes. Pinned here:

* switch off: ``work_ledger_read`` / ``work_ledger_record`` answer exactly as before
  (no new row field, no peer call), and an item with no crew serializes as before;
* a peer report shows on the hub's next read once, not twice, and earlier reports
  since the last read are filed in order;
* a running remote worker is not stale and raises no stall wake; a closed one is;
* ``accept`` promotes an ingested ``pr``;
* tunnel down or version skew keeps the last state and says why, never a status;
* a peer that does not echo ``work_item_id`` has its slot closed and is refused;
* ``decide`` reaches the peer copy, and a failed PATCH is reported, the local
  decision standing;
* a rebuild from the crew log keeps the crew pair and the ingested report.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import work_ledger_projection_helpers as helpers
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from work_ledger_projection_helpers import _SLOTS, ACCEPTANCE, CONDUCTOR, _record

import kiro_crew
from kiro_crew import work_ledger
from kiro_crew.dashboard.handlers import remote_dispatch_routes as crew_routes
from kiro_crew.dashboard.handlers import remote_ingest
from kiro_crew.dashboard.handlers import work_ledger as routes
from kiro_crew.probes.work_ledger import WorkLedgerProbe

_isolated = helpers._isolated
recorded = helpers.recorded

CREW = "inst-lab"
PEER_SLOT = "chat-77-1793000000"
PLANTED = "ghp_" + "Ab3x9" * 7 + "Z"


class _Content:
    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    async def iter_any(self):
        yield self._raw

    async def iter_chunked(self, _n: int):
        yield self._raw


class StubCrew:
    """The carrier methods the hub calls, with the peer's answers as plain data."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.state = "connected"
        self.version = kiro_crew.__version__
        self.slots: list[dict[str, Any]] = [{"key": PEER_SLOT, "running": False}]
        self.work_item: dict[str, Any] = {"item_id": "", "state": "open", "status": None}
        self.events: list[dict[str, Any]] = []
        self.patch_status = 200
        self.create_echo = True

    def status(self, instance_id: str):
        return SimpleNamespace(state=SimpleNamespace(value=self.state))

    async def peer_capability(self, instance_id: str, path: str):
        if self.state != "connected":
            return False, {"code": "capability_peer_not_connected"}
        return True, {"version": self.version}

    def _answer(self, method: str, path: str, body: Any) -> tuple[int, Any]:
        if method == "GET" and path == "api/chat/slots":
            return 200, self.slots
        if method == "POST" and path == "api/chat/slots":
            reply: dict[str, Any] = {"key": PEER_SLOT}
            if self.create_echo:
                reply["work_item_id"] = body["work_item"]["item_id"]
            return 200, reply
        if path.endswith("/work-item") and method == "GET":
            return 200, {"work_item": self.work_item, "events": self.events}
        if path.endswith("/work-item") and method == "PATCH":
            return self.patch_status, {"ok": self.patch_status == 200, "code": "nope"}
        return 200, {"ok": True}

    @asynccontextmanager
    async def proxy_request(
        self, instance_id, method, path, *, params=None, data=None, content_type=""
    ):
        from kiro_crew.instances.ssh_tunnel_manager import ProxyRequestError

        if self.state != "connected":
            raise ProxyRequestError("proxy_peer_not_connected", "down", http_status=503)
        body = json.loads(data) if data else None
        self.calls.append({"method": method, "path": path, "params": params, "body": body})
        status, reply = self._answer(method, path, body)
        yield SimpleNamespace(
            status=status,
            headers={"Content-Type": "application/json"},
            content=_Content(json.dumps(reply).encode()),
        )

    def report(self, status: str, summary: str, ts: str, **fields: Any) -> None:
        """The peer worker files a report: its item fields and one event."""
        # Every report replaces the reason, as the peer's own store does.
        self.work_item.update(
            {"status": status, "summary": summary, "last_report_at": ts, "reason": None, **fields}
        )
        # As the peer's own store keeps its log: a content-addressed id (a line
        # written twice collapses), and two progress reports in a row coalesce.
        event_id = work_ledger.event_id(
            ts, self.work_item["item_id"], "report", summary, status=status
        )
        if any(e["id"] == event_id for e in self.events):
            return
        if status == "progress" and self.events and self.events[-1]["status"] == "progress":
            self.events.pop()
        self.events.append(
            {
                "id": event_id,
                "ts": ts,
                "kind": "report",
                "status": status,
                "text": summary,
                "item_id": self.work_item["item_id"],
            }
        )


@pytest.fixture
def peer(monkeypatch) -> StubCrew:
    stub = StubCrew()
    monkeypatch.setattr("kiro_crew.remote_dispatch.remote_dispatch_enabled", lambda: True)
    return stub


def _state(peer: StubCrew | None) -> Any:
    state = MagicMock()
    state.get_slot = MagicMock(side_effect=lambda key: _SLOTS.get(key))
    state.slot_exists = MagicMock(side_effect=lambda key: key in _SLOTS)
    state.instances_manager = peer
    return state


def _req(method: str, path: str, peer: StubCrew | None, *, body: Any = ..., query: str = ""):
    app = web.Application()
    app["state"] = _state(peer)
    req = make_mocked_request(
        method,
        path + (f"?{query}" if query else ""),
        app=app,
        headers={"X-Session-Key": CONDUCTOR},
    )
    req["internal_auth"] = True
    if body is not ...:
        req.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
    return req


async def _read(peer: StubCrew | None, query: str = "") -> dict[str, Any]:
    resp = await routes.api_work_ledger_get(_req("GET", "/api/work-ledger", peer, query=query))
    assert resp.status == 200, resp.text
    return json.loads(resp.text)


async def _record_via(peer: StubCrew | None, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    resp = await routes.api_work_ledger_record(
        _req("POST", "/api/work-ledger/record", peer, body=body)
    )
    return resp.status, json.loads(resp.text)


async def _remote_item(peer: StubCrew) -> str:
    """goal + create through the routes, then the bound crew create."""
    status, body = await _record(CONDUCTOR, {"action": "goal", "goal": "ship it", "round": 1})
    assert status == 200, body
    status, body = await _record(
        CONDUCTOR, {"action": "create", "title": "item one", "acceptance": ACCEPTANCE}
    )
    assert status == 200, body
    item_id = body["item"]["item_id"]
    slot, _payload, item = await remote_ingest.create_bound(
        _state(peer), peer, CONDUCTOR, CREW, item_id, {"memory_mode": "persistent"}
    )
    assert slot == PEER_SLOT
    assert item.worker_instance_id == CREW and item.worker_session_key == PEER_SLOT
    peer.work_item["item_id"] = item_id
    return item_id


def _row(board: dict[str, Any], item_id: str) -> dict[str, Any]:
    return next(row for row in board["items"] if row["item_id"] == item_id)


# ── switch off: nothing changes ──────────────────────────────────────────────


#: The read row of a local item on main: every field, every derived flag.
_MAIN_ROW_KEYS = {
    *work_ledger.WorkItem().to_dict(),
    "orphaned",
    "stale",
    "acceptance_concrete",
    "unpatrolled",
    "pending_overdue",
    "report_undelivered",
    "events",
}


def test_an_item_with_no_crew_serializes_exactly_as_before():
    item = work_ledger.WorkItem(item_id="it_00000001", title="t")
    assert "worker_instance_id" not in item.to_dict()
    assert work_ledger.WorkItem.from_dict(item.to_dict()).to_dict() == item.to_dict()
    remote = work_ledger.WorkItem(item_id="it_00000001", worker_instance_id=CREW)
    assert remote.to_dict()["worker_instance_id"] == CREW
    assert work_ledger.WorkItem.from_dict(remote.to_dict()).worker_instance_id == CREW


def test_switch_off_read_and_record_answer_as_before_with_no_peer_call(recorded, monkeypatch):
    monkeypatch.setattr("kiro_crew.remote_dispatch.remote_dispatch_enabled", lambda: False)
    stub = StubCrew()

    async def go() -> None:
        item_id = await helpers._board()
        board = await _read(stub)
        assert set(_row(board, item_id)) == _MAIN_ROW_KEYS
        compact = await _read(stub, "compact=true")
        assert set(_row(compact, item_id)) == set(routes._COMPACT_ROW_FIELDS)
        status, body = await _record_via(
            stub, {"action": "decide", "item_id": item_id, "decision": "go"}
        )
        assert status == 200 and "remote_decide" not in body
        for _unit, entry in recorded:
            assert "worker_instance_id" not in entry

    asyncio.run(go())
    assert stub.calls == []


def test_switch_off_never_pulls_even_a_remote_item(recorded, peer, monkeypatch):
    async def go() -> None:
        item_id = await _remote_item(peer)
        peer.calls.clear()
        monkeypatch.setattr("kiro_crew.remote_dispatch.remote_dispatch_enabled", lambda: False)
        peer.report("progress", "half way", "2026-10-29T10:00:00+00:00")
        board = await _read(peer)
        assert _row(board, item_id)["status"] is None
        status, body = await _record_via(
            peer, {"action": "decide", "item_id": item_id, "decision": "go"}
        )
        assert status == 200 and "remote_decide" not in body

    asyncio.run(go())
    assert peer.calls == []


# ── ingest on read ───────────────────────────────────────────────────────────


def test_a_peer_report_shows_on_the_next_read_once_not_twice(recorded, peer):
    async def go() -> None:
        item_id = await _remote_item(peer)
        peer.report("progress", "half way", "2026-10-29T10:00:00+00:00", pr=4242)
        first = _row(await _read(peer), item_id)
        assert first["status"] == "progress" and first["summary"] == "half way"
        assert first["pr"] == 4242
        assert first["last_report_at"] == "2026-10-29T10:00:00+00:00"
        assert first["crew_state"] == "connected" and first["worker_instance_id"] == CREW
        reports = [e for e in first["events"] if e["kind"] == "report"]
        second = _row(await _read(peer), item_id)
        assert [e for e in second["events"] if e["kind"] == "report"] == reports
        assert len(reports) == 1
        entries = [d for _u, d in recorded if d.get("actor") == "worker"]
        assert len(entries) == 1 and entries[0]["board_copy"] is True
        assert entries[0]["by"] == PEER_SLOT

    asyncio.run(go())


def test_a_planted_credential_in_a_peer_report_reads_redacted(recorded, peer):
    async def go() -> None:
        item_id = await _remote_item(peer)
        peer.report("progress", f"token {PLANTED}", "2026-10-29T10:00:00+00:00")
        row = _row(await _read(peer), item_id)
        assert PLANTED not in json.dumps(row)

    asyncio.run(go())


def test_a_malformed_peer_report_is_never_filed(recorded, peer):
    async def go() -> None:
        item_id = await _remote_item(peer)
        peer.report("finished", "not a status", "2026-10-29T10:00:00+00:00")
        row = _row(await _read(peer), item_id)
        assert row["status"] is None and row["remote_error"] == "crew_report_malformed"

    asyncio.run(go())


def test_a_peer_report_whose_last_report_at_is_not_a_timestamp_is_never_filed(recorded, peer):
    async def go() -> None:
        item_id = await _remote_item(peer)
        peer.report("done", "shipped", "yesterday-ish")
        row = _row(await _read(peer), item_id)
        assert row["status"] is None and row["last_report_at"] is None
        assert row["remote_error"] == "crew_report_malformed"

    asyncio.run(go())


def test_apply_remote_report_refuses_a_stamp_that_is_not_a_timestamp(recorded, peer):
    async def go() -> None:
        item_id = await _remote_item(peer)
        with pytest.raises(work_ledger.WorkLedgerError) as exc:
            work_ledger.apply_remote_report(
                CONDUCTOR, item_id, status="done", summary="s", reported_at="not a time"
            )
        assert exc.value.code == work_ledger.CODE_INVALID_VALUE
        assert work_ledger.read_work_item(CONDUCTOR, item_id).last_report_at is None

    asyncio.run(go())


def test_accept_promotes_an_ingested_pr(recorded, peer):
    async def go() -> None:
        item_id = await _remote_item(peer)
        peer.report("done", "shipped", "2026-10-29T10:00:00+00:00", pr=4242)
        row = _row(await _read(peer), item_id)
        status, body = await _record_via(
            peer,
            {
                "action": "accept",
                "item_id": item_id,
                "acceptance": {"kind": "pr_checks", "pr": row["pr"], "repo": "o/r"},
            },
        )
        assert status == 200, body
        assert body["item"]["acceptance"]["pr"] == 4242

    asyncio.run(go())


# ── liveness: stale and the wake gate ────────────────────────────────────────


def _stall_wake(item: work_ledger.WorkItem) -> bool:
    probe = WorkLedgerProbe(worker_running=lambda k: False, worker_closed=lambda k: True)
    return work_ledger.is_stale(
        item,
        **dict(
            zip(
                ("worker_running", "worker_closed", "worker_looping"),
                work_ledger.item_liveness(
                    CONDUCTOR,
                    item,
                    running=probe._worker_running,
                    closed=probe._worker_closed,
                    looping=probe._worker_looping,
                ),
            )
        ),
        window_secs=0,
    )


def test_a_running_remote_worker_is_not_stale_and_raises_no_stall_wake(recorded, peer):
    async def go() -> None:
        item_id = await _remote_item(peer)
        peer.slots = [{"key": PEER_SLOT, "running": True, "pending_approval": True}]
        peer.report("progress", "building", "2020-01-01T00:00:00+00:00")
        row = _row(await _read(peer), item_id)
        assert row["stale"] is False and row["pending_approval"] is True
        item = work_ledger.read_work_item(CONDUCTOR, item_id)
        # The local slot table would call the peer key gone; the stored answer wins.
        assert _stall_wake(item) is False

    asyncio.run(go())


def test_a_remote_slot_missing_from_a_good_list_reads_closed(recorded, peer):
    async def go() -> None:
        item_id = await _remote_item(peer)
        peer.report("progress", "half way", "2020-01-01T00:00:00+00:00")
        peer.slots = []
        row = _row(await _read(peer), item_id)
        assert row["stale"] is True
        assert work_ledger.remote_liveness(
            CONDUCTOR, work_ledger.read_work_item(CONDUCTOR, item_id)
        ) == (False, True)

    asyncio.run(go())


def test_no_ingest_yet_reads_neither_running_nor_closed():
    item = work_ledger.WorkItem(item_id="it_0000000a", worker_instance_id=CREW)
    assert work_ledger.remote_liveness(CONDUCTOR, item) == (False, False)
    local = work_ledger.WorkItem(item_id="it_0000000a")
    assert work_ledger.remote_liveness(CONDUCTOR, local) is None


# ── fail closed ──────────────────────────────────────────────────────────────


def test_tunnel_down_keeps_the_last_state_and_says_the_crew_is_down(recorded, peer):
    async def go() -> None:
        item_id = await _remote_item(peer)
        peer.report("progress", "half way", "2026-10-29T10:00:00+00:00")
        await _read(peer)
        peer.state = "disconnected"
        peer.report("done", "never seen", "2026-10-29T11:00:00+00:00", pr=1)
        row = _row(await _read(peer), item_id)
        assert row["status"] == "progress" and row["summary"] == "half way"
        assert row["crew_state"] == "disconnected" and row["crew_down_since"]
        since = row["crew_down_since"]
        again = _row(await _read(peer, "compact=true"), item_id)
        assert again["crew_down_since"] == since and again["crew_state"] == "disconnected"
        peer.state = "connected"
        back = _row(await _read(peer), item_id)
        assert back["status"] == "done" and back["crew_down_since"] is None

    asyncio.run(go())


def test_a_version_skewed_crew_ingests_nothing(recorded, peer):
    async def go() -> None:
        item_id = await _remote_item(peer)
        peer.version = "0.0.1"
        peer.report("done", "shipped", "2026-10-29T10:00:00+00:00", pr=1)
        row = _row(await _read(peer), item_id)
        assert row["status"] is None and row["remote_error"] == "crew_version_mismatch"

    asyncio.run(go())


def test_a_peer_that_ignores_work_item_has_its_slot_closed_and_is_refused(recorded, peer):
    peer.create_echo = False

    async def go() -> None:
        await _record(CONDUCTOR, {"action": "goal", "goal": "ship it", "round": 1})
        _s, body = await _record(
            CONDUCTOR, {"action": "create", "title": "t", "acceptance": ACCEPTANCE}
        )
        item_id = body["item"]["item_id"]
        with pytest.raises(crew_routes.CrewCallError) as refused:
            await remote_ingest.create_bound(
                _state(peer), peer, CONDUCTOR, CREW, item_id, {"memory_mode": "persistent"}
            )
        assert refused.value.code == remote_ingest.CODE_PEER_LACKS_WORK_ITEMS
        assert peer.calls[-1]["method"] == "DELETE"
        assert peer.calls[-1]["path"] == f"api/chat/slots/{PEER_SLOT}"
        assert work_ledger.read_work_item(CONDUCTOR, item_id).worker_session_key is None

    asyncio.run(go())


def test_a_bound_create_refuses_an_item_already_bound(recorded, peer):
    async def go() -> None:
        item_id = await _remote_item(peer)
        peer.calls.clear()
        with pytest.raises(crew_routes.CrewCallError) as refused:
            await remote_ingest.create_bound(
                _state(peer), peer, CONDUCTOR, CREW, item_id, {"memory_mode": "persistent"}
            )
        assert refused.value.code == "already_bound"
        assert peer.calls == []

    asyncio.run(go())


def test_the_item_pair_fences_a_peer_slot(recorded, peer):
    async def go() -> None:
        item_id = await _remote_item(peer)
        assert await remote_ingest.holds_peer_slot(CONDUCTOR, CREW, PEER_SLOT)
        assert not await remote_ingest.holds_peer_slot(CONDUCTOR, "inst-other", PEER_SLOT)
        assert not await remote_ingest.holds_peer_slot(CONDUCTOR, CREW, "chat-1")
        await _record(CONDUCTOR, {"action": "close", "item_id": item_id, "state": "abandoned"})
        assert not await remote_ingest.holds_peer_slot(CONDUCTOR, CREW, PEER_SLOT)

    asyncio.run(go())


# ── decide forwards ──────────────────────────────────────────────────────────


def test_decide_reaches_the_peer_copy(recorded, peer):
    async def go() -> None:
        item_id = await _remote_item(peer)
        status, body = await _record_via(
            peer, {"action": "decide", "item_id": item_id, "decision": f"go {PLANTED}"}
        )
        assert status == 200 and body["remote_decide"] == {"ok": True}
        patch = [c for c in peer.calls if c["method"] == "PATCH"]
        assert patch[-1]["path"] == f"api/chat/slots/{PEER_SLOT}/work-item"
        assert set(patch[-1]["body"]) == {"decision"}
        assert PLANTED not in patch[-1]["body"]["decision"]

    asyncio.run(go())


def test_a_failed_patch_is_reported_and_the_local_decide_stands(recorded, peer):
    peer.patch_status = 409

    async def go() -> None:
        item_id = await _remote_item(peer)
        status, body = await _record_via(
            peer, {"action": "decide", "item_id": item_id, "decision": "go"}
        )
        assert status == 200
        assert body["remote_decide"] == {"ok": False, "code": "nope"}
        assert work_ledger.read_work_item(CONDUCTOR, item_id).decision == "go"

    asyncio.run(go())


def test_a_stalled_peer_ends_the_decide_forward_inside_its_budget(recorded, peer, monkeypatch):
    async def go() -> None:
        item_id = await _remote_item(peer)
        monkeypatch.setattr(remote_ingest, "DECIDE_DEADLINE_SECS", 0.05)
        real = peer.proxy_request
        never = asyncio.Event()

        @asynccontextmanager
        async def stalled(instance_id, method, *a, **k):
            # Connected, but the PATCH never answers: only the budget ends it.
            if method == "PATCH":
                await never.wait()
            async with real(instance_id, method, *a, **k) as up:
                yield up

        monkeypatch.setattr(peer, "proxy_request", stalled)
        status, body = await _record_via(
            peer, {"action": "decide", "item_id": item_id, "decision": "go"}
        )
        assert status == 200
        assert body["remote_decide"] == {"ok": False, "code": "crew_decide_timeout"}
        assert work_ledger.read_work_item(CONDUCTOR, item_id).decision == "go"
        assert not remote_ingest._FORWARD_LOCKS

    asyncio.run(go())


def test_the_decide_budget_fits_inside_the_record_tools_timeout():
    import ast
    import inspect

    from kiro_crew import mcp_core

    # From the source: the suite stubs ``mcp_core._post`` for every test.
    tree = ast.parse(inspect.getsource(mcp_core))
    post = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_post")
    names = [a.arg for a in post.args.kwonlyargs]
    client_timeout = ast.literal_eval(post.args.kw_defaults[names.index("timeout")])
    assert remote_ingest.DECIDE_DEADLINE_SECS + 5 < client_timeout


# ── the record holds it ──────────────────────────────────────────────────────


def test_a_rebuild_keeps_the_crew_pair_and_the_ingested_report(peer, monkeypatch):
    from kiro_crew import crew_log as lg
    from kiro_crew.crew_log import CrewLog, projection

    monkeypatch.setattr(routes.crew_log_emit, "enabled", lambda: True)
    monkeypatch.setattr(routes, "unit_for_session_key", lambda sessions, key: "u-conductor")
    CrewLog.create(
        lg.KIND_SESSION, "u-conductor", owner="raymond", agent="kirocrew", slot=CONDUCTOR
    )

    def _append(unit: str, data: dict[str, Any]) -> bool:
        projection.open_session_log(unit).append("work/recorded", data, src="gateway")
        return True

    monkeypatch.setattr(routes.crew_log_emit, "on_work_recorded", _append)

    async def go() -> None:
        item_id = await _remote_item(peer)
        peer.report("done", "shipped", "2026-10-29T10:00:00+00:00", pr=77)
        await _read(peer)
        work_ledger.item_path(CONDUCTOR, item_id).unlink()
        resp = await routes.api_work_ledger_rebuild(
            _req("POST", "/api/work-ledger/rebuild", peer, body={})
        )
        assert resp.status == 200, resp.text
        item = work_ledger.read_work_item(CONDUCTOR, item_id)
        assert item.worker_instance_id == CREW and item.worker_session_key == PEER_SLOT
        assert item.status == "done" and item.pr == 77
        assert item.last_report_at == "2026-10-29T10:00:00+00:00"
        # No binding file names the peer's key: no local session resolves this item.
        assert work_ledger.read_binding(PEER_SLOT) is None
        # And the repeat read after the rebuild files nothing new.
        before = len(work_ledger.read_events(CONDUCTOR, item_id))
        await _read(peer)
        assert len(work_ledger.read_events(CONDUCTOR, item_id)) == before

    asyncio.run(go())


def test_a_reason_left_on_a_finished_peer_item_is_dropped_not_refused(recorded, peer):
    async def go() -> None:
        item_id = await _remote_item(peer)
        peer.report("done", "shipped", "2026-10-29T10:00:00+00:00", pr=3)
        peer.work_item["reason"] = "needs_human"
        row = _row(await _read(peer), item_id)
        assert row["status"] == "done" and row["reason"] is None

    asyncio.run(go())


def test_work_item_without_crew_is_refused_by_the_tool():
    from kiro_crew.mcp_dashboard_session_runs import _run_session_create

    ctx = MagicMock()
    out = _run_session_create({"title": "t", "work_item": "it_0000abcd"}, ctx)
    assert out.startswith("Error: work_item is taken only with crew")
    ctx.client.post.assert_not_called()


def test_the_crew_schema_advertises_work_item_on_create_only():
    from kiro_crew.mcp_session_crew import with_crew_schema

    tools = [
        {"name": n, "inputSchema": {"properties": {}}} for n in ("session_create", "session_send")
    ]
    on = {t["name"]: t["inputSchema"]["properties"] for t in with_crew_schema(tools, True)}
    assert "work_item" in on["session_create"] and "work_item" not in on["session_send"]
    assert with_crew_schema(tools, False) == tools


def test_a_second_report_in_the_same_second_is_still_filed(recorded, peer):
    async def go() -> None:
        item_id = await _remote_item(peer)
        peer.report("progress", "half way", "2026-10-29T10:00:00+00:00")
        assert _row(await _read(peer), item_id)["status"] == "progress"
        peer.report("done", "shipped", "2026-10-29T10:00:00+00:00", pr=5)
        row = _row(await _read(peer), item_id)
        assert row["status"] == "done" and row["pr"] == 5
        again = _row(await _read(peer), item_id)
        assert [e["status"] for e in again["events"] if e["kind"] == "report"] == [
            "progress",
            "done",
        ]

    asyncio.run(go())


def test_one_bad_report_does_not_stall_the_reports_after_it(recorded, peer):
    async def go() -> None:
        item_id = await _remote_item(peer)
        peer.report("finished", "not a status", "2026-10-29T10:00:00+00:00")
        assert _row(await _read(peer), item_id)["status"] is None
        peer.report("done", "shipped", "2026-10-29T10:01:00+00:00", pr=6)
        row = _row(await _read(peer), item_id)
        assert row["status"] == "done" and row["pr"] == 6
        assert [e["status"] for e in row["events"] if e["kind"] == "report"] == ["done"]

    asyncio.run(go())


def test_a_slow_pull_keeps_the_crew_state_and_records_why(recorded, peer, monkeypatch):
    async def go() -> None:
        item_id = await _remote_item(peer)
        await _read(peer)
        monkeypatch.setattr(remote_ingest, "PULL_TIMEOUT_SECS", 0.01)
        real = peer.proxy_request

        never = asyncio.Event()

        @asynccontextmanager
        async def slow(*a, **k):
            # Never answers: only the pull timeout ends this call.
            await never.wait()
            async with real(*a, **k) as up:
                yield up

        monkeypatch.setattr(peer, "proxy_request", slow)
        row = _row(await _read(peer), item_id)
        assert row["crew_state"] == "connected" and row["crew_down_since"] is None
        assert row["remote_error"] == "crew_pull_timeout"

    asyncio.run(go())


def test_the_crew_board_shows_a_running_remote_worker_as_running(recorded, peer, monkeypatch):
    from kiro_crew.dashboard.handlers import work_ledger_board as board

    async def _owner(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(board, "require_owner_dashboard_request", _owner)

    async def go() -> dict[str, Any]:
        item_id = await _remote_item(peer)
        peer.slots = [{"key": PEER_SLOT, "running": True}]
        peer.report("progress", "building", "2026-10-29T10:00:00+00:00")
        await _read(peer)
        resp = await board.api_work_ledger_board(
            _req("GET", "/api/crew-board", peer, query=f"conductor={CONDUCTOR}")
        )
        assert resp.status == 200, resp.text
        rows = json.loads(resp.text)["items"]
        (row,) = [r for r in rows if r["item_id"] == item_id]
        return row

    row = asyncio.run(go())
    assert row["alive"] == "running" and row["stale"] is False


def test_a_purge_removes_the_boards_remote_records():
    from datetime import timedelta

    work_ledger.ensure_conductor(CONDUCTOR, goal="g")
    created = work_ledger.apply_conductor_action(
        CONDUCTOR, "create", title="t", acceptance=ACCEPTANCE
    )["item"]
    work_ledger.write_remote_state(CONDUCTOR, created.item_id, work_ledger.RemoteState())
    work_ledger.apply_conductor_action(
        CONDUCTOR, "close", item_id=created.item_id, state="abandoned"
    )
    path = work_ledger.remote_state_path(CONDUCTOR, created.item_id)
    assert path.exists()
    assert work_ledger.purge_conductor(CONDUCTOR, allow_unreadable=False, idle_for=timedelta(0))
    assert not path.exists()


def test_the_pull_budget_fits_inside_the_read_tools_timeout():
    import inspect

    from kiro_crew import mcp_core

    client_timeout = inspect.signature(mcp_core._get).parameters["timeout"].default
    assert remote_ingest.READ_DEADLINE_SECS + 2 < client_timeout
    assert remote_ingest.PULL_TIMEOUT_SECS <= remote_ingest.READ_DEADLINE_SECS


def test_a_pull_cancelled_while_filing_still_remembers_what_it_filed(recorded, peer, monkeypatch):
    from kiro_crew.testing.wait import async_wait_until

    real = remote_ingest._file_one
    reader: list[asyncio.Task] = []

    async def slow_file(*a, **k):
        result = await real(*a, **k)
        # Hold until the pull's timeout has cancelled the read, so the cut lands
        # after the report is written and before its cursor would be.
        await async_wait_until(lambda: reader[0].cancelling() > 0, timeout=10)
        return result

    async def go() -> None:
        await _remote_item(peer)
        await _read(peer)
        peer.report("blocked", "need a login", "2026-10-29T10:00:00+00:00")
        monkeypatch.setattr(remote_ingest, "_file_one", slow_file)
        monkeypatch.setattr(remote_ingest, "PULL_TIMEOUT_SECS", 0.05)
        task = asyncio.ensure_future(_read(peer))
        reader.append(task)
        await task
        monkeypatch.setattr(remote_ingest, "_file_one", real)
        monkeypatch.setattr(remote_ingest, "PULL_TIMEOUT_SECS", 4.0)
        await _read(peer)
        # The crew log is the record: one entry per peer report, even across a cut.
        assert [d["status"] for _u, d in recorded if d.get("actor") == "worker"] == ["blocked"]

    asyncio.run(go())


def test_a_board_with_a_remote_worker_never_holds_its_patrol_for_a_person(recorded, peer):
    from kiro_crew.probes.work_ledger import person_wait_fingerprint

    async def go() -> None:
        await _remote_item(peer)
        peer.report("blocked", "need a login", "2026-10-29T10:00:00+00:00", reason="needs_human")
        await _read(peer)

    asyncio.run(go())
    assert person_wait_fingerprint(CONDUCTOR, worker_closed=lambda k: False) is None


def test_a_same_second_report_with_new_fields_is_not_mistaken_for_one_filed(recorded, peer):
    async def go() -> None:
        item_id = await _remote_item(peer)
        await _read(peer)
        peer.report("blocked", "waiting", "2026-10-29T10:00:00+00:00")
        await _read(peer)
        # Same stamp, status and summary; new reason, artifacts and pr. The real
        # peer's event id hashes only stamp, kind, status and text, so it repeats.
        peer.report(
            "blocked",
            "waiting",
            "2026-10-29T10:00:00+00:00",
            reason="approval",
            artifacts={"pr": "12"},
            pr=12,
        )
        row = _row(await _read(peer), item_id)
        assert row["reason"] == "approval" and row["artifacts"] == {"pr": "12"} and row["pr"] == 12

    asyncio.run(go())


def test_an_event_stamped_a_second_after_its_item_is_filed_once(recorded, peer):
    async def go() -> None:
        await _remote_item(peer)
        await _read(peer)
        # The peer's item and event stamps come from two clock reads a second apart.
        peer.report("done", "shipped", "2026-10-29T10:00:01+00:00", pr=3)
        peer.work_item["last_report_at"] = "2026-10-29T10:00:00+00:00"
        await _read(peer)
        await _read(peer)
        await _read(peer)
        assert [d["status"] for _u, d in recorded if d.get("actor") == "worker"] == ["done"]

    asyncio.run(go())


def test_a_snapshot_older_than_the_items_report_is_never_filed(recorded, peer):
    async def go() -> None:
        item_id = await _remote_item(peer)
        peer.report("done", "shipped", "2026-10-29T10:01:00+00:00", pr=4)
        await _read(peer)
        # A delayed copy from before the done report.
        peer.work_item.update(
            {
                "status": "progress",
                "summary": "half way",
                "last_report_at": "2026-10-29T10:00:00+00:00",
            }
        )
        peer.events = []
        row = _row(await _read(peer), item_id)
        assert row["status"] == "done" and row["pr"] == 4

    asyncio.run(go())


def test_a_read_that_overlaps_a_pull_does_not_pull_again(recorded, peer, monkeypatch):
    async def go() -> None:
        await _remote_item(peer)
        remote_ingest._PULLING.add(CONDUCTOR)
        try:
            peer.calls.clear()
            await _read(peer)
            assert peer.calls == []
        finally:
            remote_ingest._PULLING.discard(CONDUCTOR)
        await _read(peer)
        assert peer.calls

    asyncio.run(go())


def test_a_peer_reply_report_is_ingested(recorded, peer):
    async def go() -> None:
        item_id = await _remote_item(peer)
        peer.report("reply", "the branch is wave-4", "2026-10-29T10:00:00+00:00")
        row = _row(await _read(peer), item_id)
        assert row["status"] == "reply" and row["remote_error"] is None

    asyncio.run(go())


def test_a_cut_off_read_resumes_with_the_items_pulled_longest_ago(recorded, peer, monkeypatch):
    async def go() -> list[str]:
        await _record(CONDUCTOR, {"action": "goal", "goal": "ship it", "round": 1})
        ids = []
        for n in range(3):
            _s, body = await _record(
                CONDUCTOR, {"action": "create", "title": f"t{n}", "acceptance": ACCEPTANCE}
            )
            ids.append(body["item"]["item_id"])
            work_ledger.apply_remote_bind(
                CONDUCTOR, ids[-1], worker_session_key=f"chat-{n}", worker_instance_id=CREW
            )
        order: list[str] = []

        async def one(state, mgr, board, item, slot_lists):
            order.append(item.item_id)
            record = work_ledger.read_remote_state(board, item.item_id) or work_ledger.RemoteState()
            record.attempted_at = f"2026-10-29T10:00:0{len(order)}+00:00"
            work_ledger.write_remote_state(board, item.item_id, record)

        monkeypatch.setattr(remote_ingest, "_ingest_item", one)
        monkeypatch.setattr(remote_ingest, "MAX_ITEMS_PER_READ", 2)
        await remote_ingest.ingest_board(_state(peer), CONDUCTOR)
        await remote_ingest.ingest_board(_state(peer), CONDUCTOR)
        return [*order, *ids]

    seen = asyncio.run(go())
    order, ids = seen[:4], seen[4:]
    (skipped,) = set(ids) - set(order[:2])
    # The second read starts with the item the first read never reached.
    assert order[2] == skipped and len(set(order)) == 3


def test_a_peer_event_id_too_long_to_keep_is_never_offered(recorded, peer):
    async def go() -> None:
        item_id = await _remote_item(peer)
        peer.report("blocked", "waiting", "2026-10-29T10:00:00+00:00")
        peer.report("question", "which branch", "2026-10-29T10:00:00+00:00")
        for event in peer.events:
            event["id"] = event["id"] * 8
        for _ in range(3):
            await _read(peer)
        # Only the snapshot (the newest state) is filed, once.
        assert [d["status"] for _u, d in recorded if d.get("actor") == "worker"] == ["question"]
        assert _row(await _read(peer), item_id)["status"] == "question"

    asyncio.run(go())


def test_items_whose_pulls_time_out_do_not_starve_the_rest(recorded, peer, monkeypatch):
    async def go() -> tuple[list[str], list[str]]:
        await _record(CONDUCTOR, {"action": "goal", "goal": "ship it", "round": 1})
        ids = []
        for n in range(3):
            _s, body = await _record(
                CONDUCTOR, {"action": "create", "title": f"t{n}", "acceptance": ACCEPTANCE}
            )
            ids.append(body["item"]["item_id"])
            work_ledger.apply_remote_bind(
                CONDUCTOR, ids[-1], worker_session_key=f"chat-{n}", worker_instance_id=CREW
            )
        order: list[str] = []

        async def hang(state, mgr, board, item, slot_lists):
            order.append(item.item_id)
            # Every pull hangs: only the per-item timeout ends it.
            await asyncio.Event().wait()

        monkeypatch.setattr(remote_ingest, "_ingest_item", hang)
        monkeypatch.setattr(remote_ingest, "PULL_TIMEOUT_SECS", 0.05)
        monkeypatch.setattr(remote_ingest, "READ_DEADLINE_SECS", 0.08)
        for _ in range(3):
            await remote_ingest.ingest_board(_state(peer), CONDUCTOR)
        return order, ids

    order, ids = asyncio.run(go())
    # Each read reaches fewer items than exist, yet every item is tried.
    assert set(order) == set(ids)


def test_the_crew_board_refuses_to_stop_a_remote_worker_locally(recorded, peer, monkeypatch):
    from kiro_crew.dashboard.handlers import work_ledger_board as board

    async def _owner(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(board, "require_owner_dashboard_request", _owner)
    monkeypatch.setattr(work_ledger, "is_orphaned", lambda item, **k: True)

    async def go() -> tuple[int, dict[str, Any]]:
        item_id = await _remote_item(peer)
        resp = await board.api_work_ledger_board_action(
            _req(
                "POST",
                "/api/crew-board/action",
                peer,
                body={"conductor": CONDUCTOR, "item_id": item_id, "action": "stop"},
            )
        )
        return resp.status, json.loads(resp.text)

    status, body = asyncio.run(go())
    assert status == 409 and body["code"] == "remote_worker"


@pytest.mark.parametrize(
    "remote, says",
    [
        ({"ok": True}, "reached the worker's crew"),
        ({"ok": False, "code": "proxy_peer_not_connected"}, "did NOT reach"),
    ],
)
def test_the_record_tool_says_whether_a_decide_reached_the_remote_worker(monkeypatch, remote, says):
    from kiro_crew import mcp_work

    monkeypatch.setattr(mcp_work, "_strict_caller", lambda: ("chat-1", ""))
    reply = {
        "ok": True,
        "action": "decide",
        "item": {"item_id": "it_0000abcd", "state": "open"},
        "remote_decide": remote,
    }
    monkeypatch.setattr(mcp_work, "_post", lambda *a, **k: reply)
    out = mcp_work._call_tool_inner(
        "work_ledger_record", {"action": "decide", "item_id": "it_0000abcd", "decision": "go"}
    )
    assert says in out
    if not remote["ok"]:
        assert "proxy_peer_not_connected" in out


def test_overlapping_decides_leave_the_peer_on_the_latest_decision(recorded, peer, monkeypatch):
    async def go() -> None:
        item_id = await _remote_item(peer)
        gate = asyncio.Event()
        real = peer.peer_capability
        first = [True]

        async def held(instance_id, path):
            if first[0]:
                first[0] = False
                await gate.wait()
            return await real(instance_id, path)

        monkeypatch.setattr(peer, "peer_capability", held)
        one = asyncio.ensure_future(
            _record_via(peer, {"action": "decide", "item_id": item_id, "decision": "A"})
        )
        await async_wait_until(lambda: not first[0], timeout=10)
        two = asyncio.ensure_future(
            _record_via(peer, {"action": "decide", "item_id": item_id, "decision": "B"})
        )
        # B has either delivered (no ordering) or is queued behind A's forward.
        await async_wait_until(
            lambda: two.done()
            or any(getattr(lk, "_waiters", None) for lk in remote_ingest._FORWARD_LOCKS.values()),
            timeout=10,
        )
        gate.set()
        await asyncio.gather(one, two)
        patches = [c["body"]["decision"] for c in peer.calls if c["method"] == "PATCH"]
        assert patches[-1] == "B"

    from kiro_crew.testing.wait import async_wait_until

    asyncio.run(go())


def test_a_forward_never_publishes_a_decision_still_being_recorded(recorded, peer):
    from kiro_crew.dashboard.handlers.work_ledger import _board_lock

    async def go() -> None:
        item_id = await _remote_item(peer)
        await _record_via(peer, {"action": "decide", "item_id": item_id, "decision": "A"})
        peer.calls.clear()
        async with _board_lock(CONDUCTOR):
            # A write in flight: B is in the cache, its crew-log append not done.
            item = work_ledger.read_work_item(CONDUCTOR, item_id)
            settled = item.decision
            item.decision = "B"
            work_ledger._write_record(work_ledger.item_path(CONDUCTOR, item_id), item.to_dict())
            forward = asyncio.ensure_future(
                remote_ingest.forward_decide(_state(peer), CONDUCTOR, item_id)
            )
            await asyncio.sleep(0)
            # The append failed: the write is undone.
            item.decision = settled
            work_ledger._write_record(work_ledger.item_path(CONDUCTOR, item_id), item.to_dict())
        assert (await forward)["ok"] is True
        patches = [c["body"]["decision"] for c in peer.calls if c["method"] == "PATCH"]
        assert patches == ["A"]

    asyncio.run(go())


def test_a_decide_recorded_during_the_create_reaches_the_new_worker(recorded, peer):
    async def go() -> None:
        await _record(CONDUCTOR, {"action": "goal", "goal": "ship it", "round": 1})
        _s, body = await _record(
            CONDUCTOR, {"action": "create", "title": "t", "acceptance": ACCEPTANCE}
        )
        item_id = body["item"]["item_id"]
        real = peer._answer

        def answer(method, path, body):
            if method == "POST" and path == "api/chat/slots":
                # The conductor decides while the peer create is in flight.
                work_ledger.apply_conductor_action(
                    CONDUCTOR, "decide", item_id=item_id, decision="new plan"
                )
            return real(method, path, body)

        peer._answer = answer
        await remote_ingest.create_bound(
            _state(peer), peer, CONDUCTOR, CREW, item_id, {"memory_mode": "persistent"}
        )
        patches = [c["body"]["decision"] for c in peer.calls if c["method"] == "PATCH"]
        assert patches == ["new plan"]

    asyncio.run(go())


def test_the_post_bind_check_compares_the_settled_decision(recorded, peer):
    from kiro_crew.dashboard.handlers.work_ledger import _board_lock

    async def go() -> None:
        item_id = await _remote_item(peer)
        # The hub settled on B; the create had sent A.
        await _record_via(peer, {"action": "decide", "item_id": item_id, "decision": "B"})
        peer.calls.clear()
        async with _board_lock(CONDUCTOR):
            # Another decide's tentative A is in the cache, its append not done.
            item = work_ledger.read_work_item(CONDUCTOR, item_id)
            item.decision = "A"
            work_ledger._write_record(work_ledger.item_path(CONDUCTOR, item_id), item.to_dict())
            check = asyncio.ensure_future(
                remote_ingest.forward_decide(_state(peer), CONDUCTOR, item_id, unless="A")
            )
            await asyncio.sleep(0)
            # That append failed: rolled back to B.
            item.decision = "B"
            work_ledger._write_record(work_ledger.item_path(CONDUCTOR, item_id), item.to_dict())
        assert (await check)["ok"] is True
        assert [c["body"]["decision"] for c in peer.calls if c["method"] == "PATCH"] == ["B"]
        # Equal to what the create sent: nothing to send.
        peer.calls.clear()
        out = await remote_ingest.forward_decide(_state(peer), CONDUCTOR, item_id, unless="B")
        assert out == {"ok": True} and not [c for c in peer.calls if c["method"] == "PATCH"]

    asyncio.run(go())


def test_a_bound_create_never_sends_an_acceptance_still_being_recorded(recorded, peer):
    from kiro_crew.dashboard.handlers.work_ledger import _board_lock

    async def go() -> None:
        await _record(CONDUCTOR, {"action": "goal", "goal": "ship it", "round": 1})
        _s, body = await _record(
            CONDUCTOR, {"action": "create", "title": "t", "acceptance": ACCEPTANCE}
        )
        item_id = body["item"]["item_id"]
        async with _board_lock(CONDUCTOR):
            # An accept in flight: its new bar is in the cache, its append not done.
            item = work_ledger.read_work_item(CONDUCTOR, item_id)
            item.acceptance = {"kind": "pr_checks", "pr": 1, "repo": "o/r"}
            work_ledger._write_record(work_ledger.item_path(CONDUCTOR, item_id), item.to_dict())
            create = asyncio.ensure_future(
                remote_ingest.create_bound(
                    _state(peer), peer, CONDUCTOR, CREW, item_id, {"memory_mode": "persistent"}
                )
            )
            await asyncio.sleep(0)
            # That append failed: the bar is put back.
            item.acceptance = dict(ACCEPTANCE)
            work_ledger._write_record(work_ledger.item_path(CONDUCTOR, item_id), item.to_dict())
        await create
        (post,) = [c for c in peer.calls if c["method"] == "POST"]
        assert post["body"]["work_item"]["acceptance"] == ACCEPTANCE

    asyncio.run(go())


def test_a_pull_files_nothing_once_the_item_names_another_crew(recorded, peer, monkeypatch):
    async def go() -> None:
        item_id = await _remote_item(peer)
        peer.report("done", "shipped", "2026-10-29T10:00:00+00:00", pr=2)
        real = crew_routes.peer_call

        async def rebinding(mgr, instance_id, method, path, **k):
            answer = await real(mgr, instance_id, method, path, **k)
            if path.endswith("/work-item") and method == "GET":
                # The item moved to another crew while this pull was in flight.
                item = work_ledger.read_work_item(CONDUCTOR, item_id)
                item.worker_instance_id = "inst-other"
                work_ledger._write_record(work_ledger.item_path(CONDUCTOR, item_id), item.to_dict())
            return answer

        monkeypatch.setattr(crew_routes, "peer_call", rebinding)
        await _read(peer)
        assert not [d for _u, d in recorded if d.get("actor") == "worker"]

    asyncio.run(go())


def test_only_the_latest_peer_report_is_filed_between_two_reads(recorded, peer):
    """Worker fields are latest-value: statuses the worker passed through between
    two hub reads are not kept (a Known limit), and the latest lands with all its
    fields."""

    async def go() -> None:
        item_id = await _remote_item(peer)
        peer.report("blocked", "need a login", "2026-10-29T10:00:00+00:00", reason="needs_human")
        peer.report("done", "shipped", "2026-10-29T10:05:00+00:00", artifacts={"pr": "9"}, pr=9)
        row = _row(await _read(peer), item_id)
        assert [e["status"] for e in row["events"] if e["kind"] == "report"] == ["done"]
        assert row["status"] == "done" and row["artifacts"] == {"pr": "9"} and row["pr"] == 9

    asyncio.run(go())


def test_overlapping_reads_file_the_latest_report_once(recorded, peer):
    async def go() -> None:
        item_id = await _remote_item(peer)
        peer.report("done", "shipped", "2026-10-29T10:00:00+00:00", pr=8)
        await asyncio.gather(_read(peer), _read(peer), _read(peer))
        row = _row(await _read(peer), item_id)
        assert [e["status"] for e in row["events"] if e["kind"] == "report"] == ["done"]

    asyncio.run(go())


def test_an_unwritable_remote_record_never_files_a_report_twice(recorded, peer, monkeypatch):
    """No cursor to lose: the dedupe is the hub item itself, so a remote record that
    will not write costs only liveness, never a second filing."""

    def broken(*a, **k):
        raise OSError("read-only")

    async def go() -> None:
        await _remote_item(peer)
        peer.report("blocked", "need a login", "2026-10-29T10:00:00+00:00")
        monkeypatch.setattr(work_ledger, "write_remote_state", broken)
        for _ in range(3):
            await _read(peer)
        assert [d["status"] for _u, d in recorded if d.get("actor") == "worker"] == ["blocked"]

    asyncio.run(go())


def test_a_non_string_peer_event_id_is_never_read(recorded, peer):
    async def go() -> None:
        item_id = await _remote_item(peer)
        peer.report("done", "shipped", "2026-10-29T10:00:00+00:00", pr=3)
        peer.events.append({"id": {}, "ts": [], "kind": "report", "status": "done"})
        for _ in range(2):
            await _read(peer)
        row = _row(await _read(peer), item_id)
        assert row["status"] == "done" and row["remote_error"] is None
        assert [d["status"] for _u, d in recorded if d.get("actor") == "worker"] == ["done"]

    asyncio.run(go())


def _mailbox_request(item_id: str):
    from kiro_crew import remote_dispatch_mailbox as mailbox

    return mailbox.parse_work_item(
        {
            "conductor_key": "chat-1-hub",
            "item_id": item_id,
            "title": "t",
            "acceptance": {"kind": "human_approval"},
            "decision": "",
        }
    )


def test_a_failed_bind_then_a_retry_to_the_same_crew_succeeds():
    """The hub's bind failed, so it closed the peer slot before any report: the
    copy is discarded, and the same item can be dispatched to this crew again."""
    from kiro_crew import remote_dispatch_mailbox as mailbox

    parsed = _mailbox_request("it_0000d00d")
    mailbox.open_mailbox(parsed, "chat-70-peer")
    assert mailbox.close_mailbox("chat-70-peer") == "it_0000d00d"
    assert not mailbox.item_exists(parsed)
    assert work_ledger.read_binding("chat-70-peer") is None
    again = mailbox.open_mailbox(parsed, "chat-71-peer")
    assert again.state == "open" and again.worker_session_key.endswith("chat-71-peer")


def test_a_copy_that_reported_is_still_closed_not_discarded():
    from kiro_crew import remote_dispatch_mailbox as mailbox

    parsed = _mailbox_request("it_0000feed")
    mailbox.open_mailbox(parsed, "chat-72-peer")
    work_ledger.apply_worker_report(parsed.board, parsed.item_id, status="done", summary="x", pr=1)
    assert mailbox.close_mailbox("chat-72-peer") == "it_0000feed"
    item = work_ledger.read_work_item(parsed.board, parsed.item_id)
    assert item.state == "abandoned" and item.status == "done"
    with pytest.raises(work_ledger.WorkLedgerError) as refused:
        mailbox.open_mailbox(parsed, "chat-73-peer")
    assert refused.value.code == work_ledger.CODE_DUPLICATE_ITEM


def test_a_discard_that_cannot_delete_the_item_closes_it_instead(monkeypatch):
    from pathlib import Path

    from kiro_crew import remote_dispatch_mailbox as mailbox

    parsed = _mailbox_request("it_0000abba")
    mailbox.open_mailbox(parsed, "chat-74-peer")
    target = work_ledger.item_path(parsed.board, parsed.item_id)
    real_unlink = Path.unlink

    def refuse(self, *a, **k):
        if self == target:
            raise PermissionError("denied")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", refuse)
    assert mailbox.close_mailbox("chat-74-peer") == "it_0000abba"
    monkeypatch.setattr(Path, "unlink", real_unlink)
    item = work_ledger.read_work_item(parsed.board, parsed.item_id)
    # Not half-removed: the copy is closed, and its binding still names it.
    assert item is not None and item.state == "abandoned"
    assert work_ledger.read_binding("chat-74-peer") == (parsed.board, parsed.item_id)
