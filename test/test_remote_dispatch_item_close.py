"""Closing a crew-bound item closes its peer worker, owner-safe (rfc-conductor-remote-dispatch §5c).

A write that closes a remote item ends the worker's grant (``PATCH trust: false``)
and moves the binding into ``retired_bindings`` as ``retired``, the durable
pending close the close sweep drains. Pinned here:

* a close in a turn the owner sent DELETEs the peer slot and reads ``destroyed``;
* a close in any other turn (a patrol wake) sends the PATCH and no DELETE while the
  worker is running, or idle without a ``done``; the row counts it ``close_pending``;
* once the crew lists that slot not running and its copy last reported ``done``,
  the next sweep past the backoff DELETEs it;
* after a hub restart (no backoff, no dispatch record in memory) the boot sweep
  drains an owner close, still holds a wake close on a running worker, and the
  owner's ``session_close(crew=)`` may still name the slot;
* the store admits a close reason only on a closed item, and only a close reason.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import test_remote_dispatch_hub_ingest as hub
import work_ledger_projection_helpers as helpers
from test_remote_dispatch_hub_ingest import (
    CREW,
    PEER_SLOT,
    StubCrew,
    _read,
    _record_via,
    _remote_item,
    _row,
    _state,
)
from test_remote_dispatch_retire import _closes_done, _no_background_puller, clock
from work_ledger_projection_helpers import CONDUCTOR

from kiro_crew import work_ledger
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.handlers import remote_dispatch_routes as crew_routes
from kiro_crew.dashboard.handlers import remote_ingest, remote_puller

_isolated = helpers._isolated
recorded = hub.recorded
__all__ = ["_isolated", "_no_background_puller", "clock", "recorded"]

_DELETE = f"api/chat/slots/{PEER_SLOT}"
_PATCH = f"api/chat/slots/{PEER_SLOT}/work-item"

#: The verbs the stand-in gate was asked for, newest last.
GATED: list[str] = []


@pytest.fixture
def peer(monkeypatch, clock) -> StubCrew:
    stub = StubCrew()
    stub.clock = clock  # type: ignore[attr-defined]
    monkeypatch.setattr("kiro_crew.remote_dispatch.remote_dispatch_enabled", lambda: True)
    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)
    GATED.clear()
    return stub


@pytest.fixture
def owner_turn(monkeypatch) -> None:
    async def _admit(request: Any, verb: str) -> str:
        GATED.append(verb)
        return CONDUCTOR

    monkeypatch.setattr(crew_routes, "authorize_remote_dispatch", _admit)


@pytest.fixture
def wake_turn(monkeypatch) -> None:
    async def _refuse(request: Any, verb: str) -> str:
        GATED.append(verb)
        raise crew_routes.CrewCallError(
            403, crew_routes.OWNER_UNVERIFIABLE, "crew= is open only to a turn the owner sent"
        )

    monkeypatch.setattr(crew_routes, "authorize_remote_dispatch", _refuse)


def _calls(peer: StubCrew) -> list[tuple[str, str, Any]]:
    return [
        (c["method"], c["path"], c["body"])
        for c in peer.calls
        if c["method"] in ("PATCH", "DELETE")
    ]


def _deletes(peer: StubCrew) -> list[str]:
    return [c["path"] for c in peer.calls if c["method"] == "DELETE"]


def _item(item_id: str) -> work_ledger.WorkItem:
    item = work_ledger.read_work_item(CONDUCTOR, item_id)
    assert item is not None
    return item


def _entry(item_id: str) -> dict[str, Any]:
    (entry,) = _item(item_id).retired_bindings
    return entry


async def _close(peer: StubCrew, item_id: str) -> None:
    status, body = await _record_via(
        peer, {"action": "close", "item_id": item_id, "state": "abandoned"}
    )
    assert status == 200, body
    await _closes_done()


async def _sweep(peer: StubCrew) -> None:
    await remote_puller.sweep_closes(_state(peer))
    await _closes_done()


def test_an_owner_close_deletes_the_peer_slot(recorded, peer, owner_turn):
    async def go() -> None:
        item_id = await _remote_item(peer)
        peer.slots = [{"key": PEER_SLOT, "running": True}]
        peer.calls.clear()
        await _close(peer, item_id)
        assert GATED == ["item_close"]
        assert _calls(peer)[0] == ("PATCH", _PATCH, {"trust": False})
        assert _deletes(peer) == [_DELETE]
        item = _item(item_id)
        assert item.state == "abandoned" and item.worker_session_key is None
        entry = _entry(item_id)
        assert entry["reason"] == "owner_closed" and entry["state"] == "destroyed"
        kinds = [e.kind for e in work_ledger.read_events(CONDUCTOR, item_id)]
        assert kinds[-2:] == ["retire", "destroy"]
        assert _row(await _read(peer, "compact=true"), item_id)["close_pending"] == 0

    asyncio.run(go())


@pytest.mark.parametrize(
    ("running", "status"), [(True, "done"), (True, "progress"), (False, "progress")]
)
def test_a_wake_close_never_deletes_a_worker_not_idle_and_done(
    recorded, peer, wake_turn, running, status
):
    async def go() -> None:
        item_id = await _remote_item(peer)
        peer.slots = [{"key": PEER_SLOT, "running": running}]
        peer.work_item["status"] = status
        peer.calls.clear()
        await _close(peer, item_id)
        assert GATED == ["item_close"]
        calls = _calls(peer)
        assert calls and all(c == ("PATCH", _PATCH, {"trust": False}) for c in calls)
        assert _deletes(peer) == []
        entry = _entry(item_id)
        assert entry["reason"] == "item_closed" and entry["state"] == "retired"
        assert _row(await _read(peer, "compact=true"), item_id)["close_pending"] == 1
        # Later sweeps, past every backoff, still send no DELETE.
        for _ in range(3):
            peer.clock.now += remote_ingest.CLOSE_RETRY_MAX_SECS  # type: ignore[attr-defined]
            await _sweep(peer)
        assert _deletes(peer) == []
        assert _entry(item_id)["state"] == "retired"

    asyncio.run(go())


def test_a_wake_close_deletes_once_the_worker_is_idle_and_done(recorded, peer, wake_turn):
    async def go() -> None:
        item_id = await _remote_item(peer)
        peer.slots = [{"key": PEER_SLOT, "running": True}]
        await _close(peer, item_id)
        assert _deletes(peer) == []
        # The worker finishes on its crew after the hub closed the item.
        peer.slots = [{"key": PEER_SLOT, "running": False}]
        peer.report("done", "pushed", "2026-11-18T12:00:00+00:00")
        # Inside the backoff nothing is sent.
        peer.calls.clear()
        await _sweep(peer)
        assert peer.calls == []
        peer.clock.now += remote_ingest.CLOSE_RETRY_SECS  # type: ignore[attr-defined]
        await _sweep(peer)
        assert _deletes(peer) == [_DELETE]
        assert _entry(item_id)["state"] == "destroyed"
        assert _row(await _read(peer, "compact=true"), item_id)["close_pending"] == 0

    asyncio.run(go())


def test_a_wake_close_of_an_idle_done_worker_deletes_at_once(recorded, peer, wake_turn):
    async def go() -> None:
        item_id = await _remote_item(peer)
        peer.report("done", "pushed", "2026-11-18T12:00:00+00:00")
        await _read(peer)
        peer.calls.clear()
        await _close(peer, item_id)
        assert _deletes(peer) == [_DELETE]
        assert _entry(item_id)["state"] == "destroyed"

    asyncio.run(go())


def _restart() -> None:
    """A new hub process: no backoff in memory. Each request already builds a fresh
    gateway state, so no dispatch record carries over either."""
    remote_ingest._CLOSE_RETRY.clear()


def test_a_restart_drains_an_owner_close_made_while_the_crew_was_down(recorded, peer, owner_turn):
    async def go() -> None:
        item_id = await _remote_item(peer)
        peer.state = "disconnected"
        await _close(peer, item_id)
        entry = _entry(item_id)
        assert entry["reason"] == "owner_closed" and entry["state"] == "retired"
        assert work_ledger.boards_with_pending_close() == [CONDUCTOR]
        # The owner may still name the slot to close it.
        assert await remote_ingest.holds_retired_slot(CONDUCTOR, CREW, PEER_SLOT) is True
        _restart()
        peer.state = "connected"
        peer.slots = [{"key": PEER_SLOT, "running": True}]
        await _sweep(peer)
        assert _deletes(peer) == [_DELETE]
        assert _entry(item_id)["state"] == "destroyed"
        assert work_ledger.boards_with_pending_close() == []

    asyncio.run(go())


def test_a_restart_keeps_a_wake_close_pending_until_idle_and_done(recorded, peer, wake_turn):
    async def go() -> None:
        item_id = await _remote_item(peer)
        peer.state = "disconnected"
        await _close(peer, item_id)
        _restart()
        peer.state = "connected"
        peer.slots = [{"key": PEER_SLOT, "running": True}]
        await _sweep(peer)
        assert _deletes(peer) == []
        assert work_ledger.boards_with_pending_close() == [CONDUCTOR]
        _restart()
        peer.slots = [{"key": PEER_SLOT, "running": False}]
        peer.report("done", "pushed", "2026-11-18T12:00:00+00:00")
        await _sweep(peer)
        assert _deletes(peer) == [_DELETE]
        assert _entry(item_id)["state"] == "destroyed"

    asyncio.run(go())


def test_the_store_admits_a_close_reason_only_on_a_closed_item(recorded, peer):
    async def go() -> None:
        item_id = await _remote_item(peer)
        pair = {"worker_session_key": PEER_SLOT, "worker_instance_id": CREW}
        with pytest.raises(work_ledger.WorkLedgerError) as opened:
            work_ledger.apply_remote_retire(CONDUCTOR, item_id, reason="item_closed", **pair)
        assert opened.value.code == work_ledger.CODE_ITEM_CLOSED
        work_ledger.apply_conductor_action(CONDUCTOR, "close", item_id=item_id, state="abandoned")
        with pytest.raises(work_ledger.WorkLedgerError) as closed:
            work_ledger.apply_remote_retire(CONDUCTOR, item_id, reason="crew_unreachable", **pair)
        assert closed.value.code == work_ledger.CODE_ITEM_CLOSED
        result = work_ledger.apply_remote_retire(CONDUCTOR, item_id, reason="owner_closed", **pair)
        assert result["item"].retired_bindings[0]["state"] == "retired"

    asyncio.run(go())


def test_the_conductor_cannot_name_a_close_reason_on_retire():
    from kiro_crew.validation_parts.opt_in_server_schemas import WORK_LEDGER_RECORD_SCHEMA

    spec = next(f for f in WORK_LEDGER_RECORD_SCHEMA.fields if f.name == "reason")
    assert isinstance(spec.allowed, frozenset)
    assert not spec.allowed & work_ledger.CLOSE_RETIRE_REASONS


def _peer_answers(peer: StubCrew, fn: Any) -> None:
    real = peer._answer
    peer._answer = lambda m, p, b: fn(m, p, b) or real(m, p, b)  # type: ignore[method-assign,assignment]


@pytest.mark.parametrize(
    "case",
    [
        "pending_approval",
        "running_missing",
        "slots_unreadable",
        "slots_over_cap",
        "item_read_fails",
    ],
)
def test_a_wake_close_fails_closed_on_any_doubt(recorded, peer, wake_turn, monkeypatch, case):
    from kiro_crew.dashboard import handlers_instances_peer as peer_slots

    async def go() -> None:
        item_id = await _remote_item(peer)
        peer.report("done", "pushed", "2026-11-18T12:00:00+00:00")
        peer.slots = [{"key": PEER_SLOT, "running": False}]
        if case == "pending_approval":
            peer.slots = [{"key": PEER_SLOT, "running": False, "pending_approval": True}]
        elif case == "running_missing":
            peer.slots = [{"key": PEER_SLOT}]
        elif case == "slots_unreadable":

            async def _down(*a: Any, **k: Any) -> Any:
                raise peer_slots.PeerSlotsUnavailable("down", "down", 503, "down")

            monkeypatch.setattr(peer_slots, "read_peer_slots", _down)
        elif case == "slots_over_cap":
            real = peer_slots.read_peer_slots

            async def _capped(*a: Any, **k: Any) -> Any:
                read = await real(*a, **k)
                return read._replace(over_cap=1)

            monkeypatch.setattr(peer_slots, "read_peer_slots", _capped)
        else:
            _peer_answers(
                peer,
                lambda m, p, b: (
                    (500, {"code": "boom"}) if m == "GET" and p.endswith("/work-item") else None
                ),
            )
        peer.calls.clear()
        await _close(peer, item_id)
        assert _deletes(peer) == []
        assert _entry(item_id)["state"] == "retired"

    asyncio.run(go())


def test_a_wake_close_of_a_slot_the_crew_no_longer_lists_destroys_it(recorded, peer, wake_turn):
    async def go() -> None:
        item_id = await _remote_item(peer)
        peer.slots = []
        _peer_answers(
            peer, lambda m, p, b: (404, {"code": "slot_not_found"}) if m == "DELETE" else None
        )
        await _close(peer, item_id)
        assert _deletes(peer) == [_DELETE]
        assert _entry(item_id)["state"] == "destroyed"

    asyncio.run(go())


@pytest.mark.parametrize("gate", ["session_control_off", "gate_raises"])
def test_an_owner_gate_that_does_not_pass_reads_as_a_wake(recorded, peer, monkeypatch, gate):
    async def go() -> None:
        if gate == "session_control_off":
            monkeypatch.setattr(sc, "session_control_enabled", lambda: False)
            monkeypatch.setattr(crew_routes, "authorize_remote_dispatch", _never)
        else:

            async def _boom(request: Any, verb: str) -> str:
                raise RuntimeError("resolver fault")

            monkeypatch.setattr(crew_routes, "authorize_remote_dispatch", _boom)
        item_id = await _remote_item(peer)
        peer.slots = [{"key": PEER_SLOT, "running": True}]
        await _close(peer, item_id)
        assert _deletes(peer) == []
        assert _entry(item_id)["reason"] == "item_closed"

    async def _never(request: Any, verb: str) -> str:
        raise AssertionError("the gate must not run with session control off")

    asyncio.run(go())


def test_an_unknown_retire_reason_never_closes_a_running_worker(recorded, peer):
    async def go() -> None:
        item_id = await _remote_item(peer)
        work_ledger.apply_conductor_action(CONDUCTOR, "close", item_id=item_id, state="abandoned")
        item = _item(item_id)
        entry = {
            "worker_session_key": PEER_SLOT,
            "worker_instance_id": CREW,
            "state": "retired",
            "reason": None,
        }
        item.retired_bindings = [entry]
        peer.slots = [{"key": PEER_SLOT, "running": True}]
        remote_ingest._schedule_closes(_state(peer), CONDUCTOR, [item])
        await _closes_done()
        assert _deletes(peer) == []

    asyncio.run(go())


def test_a_close_whose_retire_does_not_land_still_ends_the_grant(recorded, peer, monkeypatch):
    async def go() -> None:
        item_id = await _remote_item(peer)

        async def _refused(*a: Any, **k: Any) -> None:
            return None

        monkeypatch.setattr(remote_ingest, "_record_retire", _refused)
        peer.calls.clear()
        await _close(peer, item_id)
        assert _calls(peer) == [("PATCH", _PATCH, {"trust": False})]
        assert _item(item_id).worker_session_key == PEER_SLOT

    asyncio.run(go())
