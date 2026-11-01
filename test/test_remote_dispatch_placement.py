"""Hub placement, wave 5 (rfc-conductor-remote-dispatch §2b).

Drives the real placement, bound create and ledger store against stub crews. Pinned:
switch off keeps today's tool refusal; most headroom after reservations wins, a
kept-open ``done`` worker still reserving; ``crew_busy`` falls through, all busy
leaves the item unbound; ineligible crews are skipped; two dispatches at once split;
any other refusal stops the placement.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
import work_ledger_projection_helpers as helpers
from work_ledger_projection_helpers import _SLOTS, ACCEPTANCE, CONDUCTOR, _record

import kiro_crew
from kiro_crew import work_ledger
from kiro_crew.dashboard.handlers import remote_dispatch_routes as routes
from kiro_crew.dashboard.handlers import remote_placement as placement
from kiro_crew.dashboard.remote_dispatch_headroom import RemoteDispatchSettings
from kiro_crew.testing.wait import await_until
from kiro_crew.work_ledger import open_remote_bindings

_isolated = helpers._isolated
recorded = helpers.recorded

_BUSY = {"code": "crew_busy", "short": "disk"}


class _Content:
    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    async def iter_any(self):
        yield self._raw


class Crews:
    """Stub crews behind one carrier, each with its own headroom and create answer."""

    def __init__(self) -> None:
        self.crews: dict[str, dict[str, Any]] = {}
        self.creates: list[str] = []
        self.gate: asyncio.Event | None = None
        self.waiting = 0
        self.both_waiting: asyncio.Event | None = None

    def add(self, iid: str, mem: float = 50.0, disk: float = 200.0, **over: Any) -> None:
        room = {"mem_free_gb": mem, "disk_free_gb": disk, "load_per_cpu": 0.2}
        self.crews[iid] = {"state": "connected", "version": kiro_crew.__version__, **over}
        self.crews[iid].setdefault("headroom", room)

    def status(self, iid: str):
        return SimpleNamespace(state=SimpleNamespace(value=self.crews[iid]["state"]))

    async def peer_capability(self, iid: str, path: str):
        return True, {"version": self.crews[iid]["version"]}

    def _answer(self, iid: str, method: str, path: str, body: Any) -> tuple[int, Any]:
        crew = self.crews[iid]
        if path == "api/chat/headroom":
            return crew.get("headroom_status", 200), crew["headroom"]
        if path != "api/chat/slots":
            return 200, {"ok": True}  # the title rename
        self.creates.append(iid)
        if crew.get("create") == "busy":
            return 503, _BUSY
        if crew.get("create") == "refused":
            return 500, {"error": "boom"}
        item_id = body["work_item"]["item_id"]
        return 200, {"key": f"chat-{iid}", "headroom": crew["headroom"], "work_item_id": item_id}

    @asynccontextmanager
    async def proxy_request(self, iid, method, path, *, params=None, data=None, content_type=""):
        if path == "api/chat/headroom" and self.crews[iid].get("stall"):
            await asyncio.sleep(60)
        if self.gate is not None and path == "api/chat/slots":
            self.waiting += 1
            if self.waiting == 2 and self.both_waiting is not None:
                self.both_waiting.set()
            await self.gate.wait()
        status, reply = self._answer(iid, method, path, json.loads(data) if data else None)
        yield SimpleNamespace(
            status=status,
            headers={"Content-Type": "application/json"},
            content=_Content(json.dumps(reply).encode()),
        )


def _settings(enabled: bool = True) -> RemoteDispatchSettings:
    return RemoteDispatchSettings(enabled=enabled, memory_gb=2.0, disk_gb=10.0)


@pytest.fixture
def crews(monkeypatch) -> Crews:
    monkeypatch.setattr(placement, "remote_dispatch_settings", _settings)
    return Crews()


def _state(crews: Crews) -> Any:
    state = MagicMock()
    state.get_slot = MagicMock(side_effect=lambda key: _SLOTS.get(key))
    state.instances_manager = crews
    rows = lambda: [SimpleNamespace(id=i, name=i) for i in crews.crews]  # noqa: E731
    state.instances_registry = SimpleNamespace(list=rows)
    return state


def _run(crews: Crews, n: int, body) -> None:
    """Open *n* items on the conductor's ledger, then ``await body(state, items)``."""

    async def go() -> None:
        status, out = await _record(CONDUCTOR, {"action": "goal", "goal": "ship", "round": 1})
        assert status == 200, out
        items = []
        for i in range(n):
            create = {"action": "create", "title": f"item {i}", "acceptance": ACCEPTANCE}
            status, out = await _record(CONDUCTOR, create)
            assert status == 200, out
            items.append(out["item"]["item_id"])
        await body(_state(crews), items)

    asyncio.run(go())


async def _place(state: Any, item_id: str) -> str:
    result, name = await placement.place(
        state, state.instances_manager, CONDUCTOR, {"work_item": item_id}, board=CONDUCTOR
    )
    assert result["crew"] == name and result["work_item_id"] == item_id
    item = work_ledger.read_work_item(CONDUCTOR, item_id)
    assert item is not None and item.worker_instance_id == name
    return name


async def _refused(state: Any, item_id: str) -> routes.CrewCallError:
    with pytest.raises(routes.CrewCallError) as exc:
        await placement.place(
            state, state.instances_manager, CONDUCTOR, {"work_item": item_id}, board=CONDUCTOR
        )
    return exc.value


# ── the switch ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("enabled", [False, True])
def test_the_tool_places_only_with_the_switch_on(monkeypatch, enabled):
    from kiro_crew.mcp_dashboard_session_runs import _run_session_create

    monkeypatch.setattr("kiro_crew.remote_dispatch.remote_dispatch_enabled", lambda: enabled)
    ctx = MagicMock()
    ctx.client.post.return_value = {
        "target": "c",
        "crew": "lab-2",
        "crew_name": "lab",
        "placed": True,
    }
    out = _run_session_create({"work_item": "it_0000abcd"}, ctx)
    if not enabled:
        assert out == (
            "Error: work_item is taken only with crew; bind a local session with "
            "work_ledger_record."
        )
        ctx.client.post.assert_not_called()
        return
    path, payload = ctx.client.post.call_args.args
    assert (path, payload) == ("/api/session-control/create", {"work_item": "it_0000abcd"})
    assert "on crew `lab`" in out and "picked this crew by headroom" in out
    # Two crews may both be named lab; only the id reaches the one chosen.
    assert "crew=`lab-2`" in out


def test_the_switch_turned_off_midway_places_nothing(recorded, crews, monkeypatch):
    crews.add("a")
    monkeypatch.setattr(placement, "remote_dispatch_settings", lambda: _settings(False))

    async def body(state, items):
        assert (await _refused(state, items[0])).code == "remote_dispatch_disabled"
        assert (await _refused(state, "")).code == "work_item_invalid"

    _run(crews, 1, body)
    assert crews.creates == []


# ── ranking and reservations ─────────────────────────────────────────────────


def test_most_headroom_wins_and_the_scarcer_resource_decides(recorded, crews):
    crews.add("a", mem=8)
    crews.add("b", mem=40)
    crews.add("c", mem=99, disk=20)  # 49 memory estimates but only 2 disk ones

    async def body(state, items):
        assert await _place(state, items[0]) == "b"

    _run(crews, 1, body)
    assert crews.creates == ["b"]


def test_open_bindings_reserve_until_closed_a_kept_open_done_one_too(recorded, crews):
    crews.add("a", mem=10)
    crews.add("b", mem=9)

    async def body(state, items):
        first, second, third = items
        assert await _place(state, first) == "a"
        # Unreserved a (10) beats b (9); with a's one binding it scores 10-2.
        assert await _place(state, second) == "b"
        work_ledger.apply_remote_report(
            CONDUCTOR, first, status="done", summary="ok", pr=1, reported_at="2026-10-31T00:00:00Z"
        )
        assert open_remote_bindings() == {"a": 1, "b": 1}
        # a 10-2=8 < b 10.5-2=8.5; were the done worker free, a would score 10.
        crews.crews["b"]["headroom"]["mem_free_gb"] = 10.5
        assert await _place(state, third) == "b"
        status, out = await _record(
            CONDUCTOR, {"action": "close", "item_id": first, "state": "abandoned"}
        )
        assert status == 200, out
        assert open_remote_bindings() == {"b": 2}

    _run(crews, 3, body)


def test_two_dispatches_at_once_do_not_both_land_on_a_crew_with_room_for_one(recorded, crews):
    # Each create waits until both placements chose, so neither sees the other's
    # binding on disk: only the in-flight count can split them.
    crews.add("a", mem=4.5)
    crews.add("b", mem=4.0)

    async def body(state, items):
        crews.gate, crews.both_waiting = asyncio.Event(), asyncio.Event()
        tasks = [asyncio.create_task(_place(state, i)) for i in items]
        await await_until(crews.both_waiting, what="both creates waiting on the gate")
        crews.gate.set()
        assert sorted(await asyncio.gather(*tasks)) == ["a", "b"]

    _run(crews, 2, body)


# ── fall-through ─────────────────────────────────────────────────────────────


def test_crew_busy_falls_through_to_the_next_crew(recorded, crews):
    crews.add("a", mem=40, create="busy")
    crews.add("b", mem=10)

    async def body(state, items):
        assert await _place(state, items[0]) == "b"

    _run(crews, 1, body)
    assert crews.creates == ["a", "b"]


def test_every_crew_busy_leaves_the_item_open_and_unbound(recorded, crews):
    crews.add("a", create="busy")
    crews.add("b", create="busy")

    async def body(state, items):
        exc = await _refused(state, items[0])
        assert (exc.status, exc.code) == (503, placement.CREW_NONE_AVAILABLE)
        assert "a turn the owner sends" in exc.message
        stored = work_ledger.read_work_item(CONDUCTOR, items[0])
        assert stored is not None and not stored.is_terminal
        assert not stored.worker_session_key and not stored.worker_instance_id

    _run(crews, 1, body)


def test_a_refusal_other_than_busy_stops_the_placement(recorded, crews):
    crews.add("a", mem=40, create="refused")
    crews.add("b", mem=10)

    async def body(state, items):
        assert (await _refused(state, items[0])).code == "crew_peer_refused"

    _run(crews, 1, body)
    assert crews.creates == ["a"]


# ── eligibility ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "over",
    [
        {"state": "disconnected"},
        {"version": "0.0.1"},
        {"headroom": {"mem_free_gb": 50, "disk_free_gb": 200, "load_per_cpu": 1.0}},
        {"headroom": {"mem_free_gb": 1.9, "disk_free_gb": 200, "load_per_cpu": 0}},
        {"headroom": {"mem_free_gb": 50, "disk_free_gb": 9.9, "load_per_cpu": 0}},
        {"headroom": {"mem_free_gb": None, "disk_free_gb": 200, "load_per_cpu": 0}},
        {"headroom": {"mem_free_gb": 10**400, "disk_free_gb": 200, "load_per_cpu": 0}},
        {"headroom_status": 404},
        {"stall": True},
    ],
)
def test_an_ineligible_crew_is_skipped(recorded, crews, monkeypatch, over):
    monkeypatch.setattr(placement, "PROBE_TIMEOUT_S", 0.2)
    crews.add("a", mem=99, **over)
    crews.add("b", mem=4)

    async def body(state, items):
        assert await _place(state, items[0]) == "b"

    _run(crews, 1, body)
    assert crews.creates == ["b"]


def test_an_unreadable_status_or_registry_places_nothing(recorded, crews, monkeypatch):
    crews.add("a")

    def _raise(*_a):
        raise RuntimeError("down")

    async def body(state, items):
        monkeypatch.setattr(crews, "status", _raise)
        assert (await _refused(state, items[0])).code == placement.CREW_NONE_AVAILABLE
        monkeypatch.setattr(routes, "_instances", _raise)
        assert (await _refused(state, items[0])).code == placement.CREW_NONE_AVAILABLE

    _run(crews, 1, body)
    assert crews.creates == []


def test_a_closed_item_is_refused_before_any_crew_is_probed(recorded, crews, monkeypatch):
    crews.add("a")
    probed: list[str] = []
    monkeypatch.setattr(placement, "_candidate", lambda *a: probed.append("x"))

    async def body(state, items):
        status, out = await _record(
            CONDUCTOR, {"action": "close", "item_id": items[0], "state": "abandoned"}
        )
        assert status == 200, out
        assert (await _refused(state, items[0])).code == work_ledger.CODE_ITEM_CLOSED

    _run(crews, 1, body)
    assert probed == [] and crews.creates == []


def test_no_new_create_starts_once_the_budget_is_spent(recorded, crews, monkeypatch):
    crews.add("a", mem=40, create="busy")
    crews.add("b", mem=10)
    # start, the check before a's create, then the check before b's create.
    clock = iter([0.0, 1.0, 99.0])
    monkeypatch.setattr(placement, "_clock", lambda: next(clock))

    async def body(state, items):
        exc = await _refused(state, items[0])
        assert exc.code == placement.CREW_NONE_AVAILABLE and "stopped after" in exc.message
        stored = work_ledger.read_work_item(CONDUCTOR, items[0])
        assert stored is not None and not stored.worker_instance_id

    _run(crews, 1, body)
    assert crews.creates == ["a"]


def test_an_unexpected_probe_error_skips_only_that_crew(recorded, crews, monkeypatch):
    crews.add("a", mem=99)
    crews.add("b", mem=4)
    real = routes.check_version

    async def _check(mgr, iid):
        if iid == "a":
            raise RuntimeError("carrier fault")
        await real(mgr, iid)

    monkeypatch.setattr(routes, "check_version", _check)

    async def body(state, items):
        assert await _place(state, items[0]) == "b"

    _run(crews, 1, body)


def test_no_create_is_sent_when_the_budget_runs_out_during_the_reservation(
    recorded, crews, monkeypatch
):
    crews.add("a")
    now = [0.0]
    monkeypatch.setattr(placement, "_clock", lambda: now[0])
    real = placement._reserve_best

    async def _slow_reserve(*args):
        now[0] = 99.0  # the lock wait ate the whole budget
        return await real(*args)

    monkeypatch.setattr(placement, "_reserve_best", _slow_reserve)

    async def body(state, items):
        exc = await _refused(state, items[0])
        assert exc.code == placement.CREW_NONE_AVAILABLE and "stopped after" in exc.message
        assert placement._INFLIGHT.get(state, {}) == {}

    _run(crews, 1, body)
    assert crews.creates == []


def test_an_explicit_create_in_flight_counts_against_its_crew(recorded, crews):
    # a: 3 GB = 1.5 estimates free, b: 2.5 GB = 1.25. With a's explicit create
    # still waiting on its peer reply, a scores 0.5 and b wins.
    crews.add("a", mem=3.0)
    crews.add("b", mem=2.5)

    async def body(state, items):
        async with placement.holding(state, "a"):
            assert placement._INFLIGHT[state] == {"a": 1}
            assert await _place(state, items[0]) == "b"
        assert placement._INFLIGHT[state] == {}

    _run(crews, 1, body)
