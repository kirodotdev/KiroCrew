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
        self.probe_cancelled = False
        self.headroom_params: list[Any] = []

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
        if path == "api/chat/headroom":
            self.headroom_params.append(params)
        if path == "api/chat/headroom" and self.crews[iid].get("stall"):
            try:
                await asyncio.Event().wait()  # blocks until cancelled by wait_for
            except asyncio.CancelledError:
                self.probe_cancelled = True
                raise
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
    ],
)
def test_an_ineligible_crew_is_skipped(recorded, crews, over):
    crews.add("a", mem=99, **over)
    crews.add("b", mem=4)

    async def body(state, items):
        assert await _place(state, items[0]) == "b"

    _run(crews, 1, body)
    assert crews.creates == ["b"]


def test_a_stalling_crew_probe_is_skipped(recorded, crews, monkeypatch):
    """A crew whose headroom probe never returns is skipped; ineligibility is
    decided by the probe stub returning _UNREAD, not by a wall-clock race."""
    crews.add("a", mem=99)
    crews.add("b", mem=4)
    real_candidate = placement._candidate

    async def _candidate_stub(state, mgr, row, mem_gb, disk_gb, requires):
        if getattr(row, "id", "") == "a":
            return placement._UNREAD  # simulates _candidate timing out on a stalled probe
        return await real_candidate(state, mgr, row, mem_gb, disk_gb, requires)

    monkeypatch.setattr(placement, "_candidate", _candidate_stub)

    async def body(state, items):
        assert await _place(state, items[0]) == "b"

    _run(crews, 1, body)
    assert crews.creates == ["b"]


def test_candidate_reads_a_stalled_probe_as_unread(crews, monkeypatch):
    """_candidate returns _UNREAD and the stall Event is cancelled when the probe
    takes longer than PROBE_TIMEOUT_S.

    Only crew 'a' is probed here, so PROBE_TIMEOUT_S applies to that one probe
    in isolation with no competitor.  The Event().wait() never returns on its
    own; wait_for fires CancelledError into it after the timeout elapses, which
    sets probe_cancelled via the except clause in proxy_request.
    """
    crews.add("a", mem=99, stall=True)
    monkeypatch.setattr(placement, "PROBE_TIMEOUT_S", 0.01)

    async def go():
        state = _state(crews)
        row = next(r for r in state.instances_registry.list() if r.id == "a")
        result = await placement._candidate(state, crews, row, 2.0, 10.0)
        assert result == placement._UNREAD
        assert crews.probe_cancelled, "wait_for must have cancelled the stalled probe"

    asyncio.run(go())


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


# ── wave 8: a retired crew is never placed on again ──────────────────────────


def test_placement_skips_a_crew_the_item_was_retired_from(recorded, crews):
    crews.add("lab-a", mem=500.0, disk=2000.0)  # the roomiest, but the item died there
    crews.add("lab-b")

    async def body(state: Any, items: list[str]) -> None:
        (item_id,) = items
        work_ledger.apply_remote_bind(
            CONDUCTOR,
            item_id,
            worker_session_key="chat-old",
            worker_instance_id="lab-a",
        )
        work_ledger.apply_remote_report(
            CONDUCTOR,
            item_id,
            status="progress",
            summary="pushed",
            artifacts={"branch": "wave8-try1"},
            reported_at="2026-10-29T10:00:00+00:00",
        )
        work_ledger.apply_remote_retire(
            CONDUCTOR,
            item_id,
            worker_session_key="chat-old",
            worker_instance_id="lab-a",
            reason="crew_unreachable",
        )
        result, name = await placement.place(
            state,
            state.instances_manager,
            CONDUCTOR,
            {"work_item": item_id},
            board=CONDUCTOR,
        )
        assert name == "lab-b" and crews.creates == ["lab-b"]
        assert result["previous_branch"] == "wave8-try1"
        item = work_ledger.read_work_item(CONDUCTOR, item_id)
        assert item.worker_instance_id == "lab-b"
        assert item.retired_bindings[0]["worker_instance_id"] == "lab-a"

    _run(crews, 1, body)


def test_placement_of_a_never_retired_item_carries_no_previous_branch(recorded, crews):
    crews.add("lab-a")

    async def body(state: Any, items: list[str]) -> None:
        result, _name = await placement.place(
            state,
            state.instances_manager,
            CONDUCTOR,
            {"work_item": items[0]},
            board=CONDUCTOR,
        )
        assert "previous_branch" not in result

    _run(crews, 1, body)


# ── requires: capability filter before ranking (audit D2) ────────────────────


async def _place_with(state: Any, item_id: str, requires: Any) -> tuple[dict[str, Any], str]:
    return await placement.place(
        state,
        state.instances_manager,
        CONDUCTOR,
        {"work_item": item_id, "requires": requires},
        board=CONDUCTOR,
    )


def _room(**caps: Any) -> dict[str, Any]:
    return {"mem_free_gb": 50.0, "disk_free_gb": 200.0, "load_per_cpu": 0.2, **caps}


@pytest.mark.parametrize(
    "raw",
    [
        "gh",
        {"logins": "gh"},
        {"tags": ["GPU"]},
        {"tags": ["/etc/passwd"]},
        {"tags": ["lab.example.com"]},
        {"tags": ["123456789012"]},
        {"tags": [1]},
        {"logins": ["aws"]},
        {"tags": [f"t{i}" for i in range(17)]},
        {"accounts": ["x"]},
    ],
)
def test_a_malformed_requires_is_refused_before_any_crew_is_probed(recorded, crews, raw):
    crews.add("a")

    async def body(state, items):
        with pytest.raises(routes.CrewCallError) as exc:
            await _place_with(state, items[0], raw)
        assert exc.value.code == placement.REQUIRES_INVALID

    _run(crews, 1, body)
    assert crews.creates == []


def test_parse_requires_keeps_order_drops_repeats_and_reads_empty_as_nothing():
    req = placement.parse_requires({"tags": ["gpu", "docker", "gpu"], "logins": ["gh"]})
    assert req == placement.Requires(tags=("gpu", "docker"), logins=("gh",))
    assert not placement.parse_requires(None)
    assert not placement.parse_requires({})


def test_a_crew_without_the_login_is_skipped_however_much_room_it_has(recorded, crews):
    crews.add("a", headroom=_room(mem_free_gb=99.0, logins={"gh": "expired"}))
    crews.add("b", headroom=_room(mem_free_gb=8.0, logins={"gh": "valid"}, tags=["gpu"]))

    async def body(state, items):
        _, name = await _place_with(state, items[0], {"logins": ["gh"], "tags": ["gpu"]})
        assert name == "b"

    _run(crews, 1, body)
    assert crews.creates == ["b"]


@pytest.mark.parametrize(
    "logins, word",
    [
        ({"gh": "expired"}, "login gh (expired)"),
        ({"gh": "unknown"}, "login gh (still checking; retry shortly)"),
        ({"gh": "VALID"}, "login gh (not reported)"),
        ({"gh": {"state": "valid"}}, "login gh (not reported)"),
        ("gh=valid", "login gh (not reported)"),
        (None, "login gh (not reported)"),
    ],
)
def test_no_qualifying_crew_is_refused_naming_what_each_lacked(recorded, crews, logins, word):
    crews.add("a", headroom=_room(logins=logins, tags="gpu"))  # tags not a list: none
    crews.add("old")  # a peer older than tags/logins reports neither

    async def body(state, items):
        with pytest.raises(routes.CrewCallError) as exc:
            await _place_with(state, items[0], {"logins": ["gh"], "tags": ["gpu"]})
        assert exc.value.code == placement.CREW_REQUIREMENTS_UNMET
        assert exc.value.status == 503
        assert f"a lacks tag gpu, {word}" in exc.value.message
        assert "old lacks tag gpu, login gh (not reported)" in exc.value.message
        item = work_ledger.read_work_item(CONDUCTOR, items[0])
        assert item is not None and not item.worker_instance_id

    _run(crews, 1, body)
    assert crews.creates == []  # no blind pick


@pytest.mark.parametrize(
    "unread",
    [{"headroom_status": 404}, {"version": "0.0.1"}],
    ids=["headroom-404", "version-mismatch"],
)
def test_a_crew_that_could_not_be_read_is_named_in_the_refusal(recorded, crews, unread):
    crews.add("a", headroom=_room(tags=["cpu"]))
    crews.add("b", headroom=_room(tags=["gpu"]), **unread)
    crews.add("c", state="disconnected", headroom=_room(tags=["gpu"]))

    async def body(state, items):
        with pytest.raises(routes.CrewCallError) as exc:
            await _place_with(state, items[0], {"tags": ["gpu"]})
        assert exc.value.code == placement.CREW_REQUIREMENTS_UNMET
        assert exc.value.status == 503
        assert "(a lacks tag gpu; b could not be checked)" in exc.value.message
        assert "c could not be checked" not in exc.value.message  # not connected
        item = work_ledger.read_work_item(CONDUCTOR, items[0])
        assert item is not None and item.state == "open" and not item.worker_instance_id

    _run(crews, 1, body)
    assert crews.creates == []


def test_a_qualifying_crew_with_no_room_answers_no_room_not_requirements(recorded, crews):
    crews.add("a", headroom=_room(mem_free_gb=1.0, tags=["gpu"]))
    crews.add("b", headroom=_room(tags=[]))

    async def body(state, items):
        with pytest.raises(routes.CrewCallError) as exc:
            await _place_with(state, items[0], {"tags": ["gpu"]})
        assert exc.value.code == placement.CREW_NONE_AVAILABLE
        assert "b lacks tag gpu" in exc.value.message

    _run(crews, 1, body)
    assert crews.creates == []


def test_requires_is_never_sent_to_the_peer(recorded, crews, monkeypatch):
    crews.add("a", headroom=_room(tags=["gpu"]))
    sent: list[Any] = []
    real = crews._answer

    def spy(iid, method, path, body):
        if path == "api/chat/slots":
            sent.append(body)
        return real(iid, method, path, body)

    monkeypatch.setattr(crews, "_answer", spy)

    async def body(state, items):
        await _place_with(state, items[0], {"tags": ["gpu"]})

    _run(crews, 1, body)
    assert sent and all("requires" not in b for b in sent)


@pytest.mark.parametrize(
    "requires, params",
    [
        (None, {"logins": "0"}),
        ({"tags": ["gpu"]}, {"logins": "0"}),
        ({"logins": ["gh"]}, {"logins": "1"}),
        ({"tags": ["gpu"], "logins": ["gh"]}, {"logins": "1"}),
    ],
)
def test_the_headroom_read_asks_for_logins_only_when_requires_names_one(
    recorded, crews, requires, params
):
    crews.add("a", headroom=_room(logins={"gh": "valid"}, tags=["gpu"]))

    async def body(state, items):
        _, name = await _place_with(state, items[0], requires)
        assert name == "a"
        if requires:
            await placement.check_crew_requires(crews, "a", "a", requires)

    _run(crews, 1, body)
    assert crews.headroom_params and all(p == params for p in crews.headroom_params)


def test_an_explicit_crew_is_checked_against_requires(crews):
    crews.add("a", headroom=_room(logins={"gh": "valid"}))
    crews.add("down", headroom={}, headroom_status=500)

    async def go():
        await placement.check_crew_requires(crews, "a", "a", None)
        await placement.check_crew_requires(crews, "a", "a", {"logins": ["gh"]})
        with pytest.raises(routes.CrewCallError) as exc:
            await placement.check_crew_requires(crews, "a", "a", {"tags": ["gpu"]})
        assert exc.value.code == placement.CREW_REQUIREMENTS_UNMET
        assert "a lacks tag gpu" in exc.value.message
        with pytest.raises(routes.CrewCallError) as exc:
            await placement.check_crew_requires(crews, "down", "down", {"tags": ["gpu"]})
        assert exc.value.code == placement.CREW_REQUIREMENTS_UNMET
        with pytest.raises(routes.CrewCallError) as exc:
            await placement.check_crew_requires(crews, "a", "a", {"logins": ["aws"]})
        assert exc.value.code == placement.REQUIRES_INVALID

    asyncio.run(go())


def test_the_tool_forwards_requires_and_refuses_it_on_a_local_create(monkeypatch):
    from kiro_crew.mcp_dashboard_session_runs import _run_session_create

    monkeypatch.setattr("kiro_crew.remote_dispatch.remote_dispatch_enabled", lambda: True)
    ctx = MagicMock()
    ctx.client.post.return_value = {"target": "c", "crew": "lab", "placed": True}
    _run_session_create({"work_item": "it_0000abcd", "requires": {"tags": ["gpu"]}}, ctx)
    _, payload = ctx.client.post.call_args.args
    assert payload == {"work_item": "it_0000abcd", "requires": {"tags": ["gpu"]}}
    ctx.client.post.reset_mock()
    out = _run_session_create({"requires": {"tags": ["gpu"]}}, ctx)
    assert out == "Error: requires is taken only with crew or work_item."
    ctx.client.post.assert_not_called()


def test_the_schema_offers_requires_on_session_create_only():
    from kiro_crew.mcp_session_crew import with_crew_schema

    rows = [{"name": "session_create"}, {"name": "session_send"}]
    out = {t["name"]: t["inputSchema"]["properties"] for t in with_crew_schema(rows, True)}
    assert out["session_create"]["requires"]["additionalProperties"] is False
    assert "requires" not in out["session_send"]
