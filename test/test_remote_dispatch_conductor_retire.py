"""The conductor's own retire of a crew-bound item (rfc-conductor-remote-dispatch §5b).

``work_ledger_record action=retire reason=login_blocked|wrong_crew`` runs the hub's
own retire write. Pinned here:

* the item stays open and unbound, with its history and the worker's last branch;
  the row says ``redispatch_due`` and names the crew placement skips;
* the old peer slot is closed at once while its crew is connected, and the slot
  leaves the dispatch record;
* a new crew binds the same item afterwards;
* only the owner may retire: another conductor's call reads the item as unknown;
* a local-bound item, an unbound one, the hub's own ``crew_unreachable`` reason and
  a call with the switch off are each refused with their own code;
* the retire passes ``session_close(crew=)``'s gate first: a turn the owner did not
  send, or ``agent.session_control`` off, is refused with the item and the peer
  slot untouched, and no ``DELETE`` reaches the peer, then or on a later pull.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock

import pytest
import test_remote_dispatch_hub_ingest as hub
import work_ledger_projection_helpers as helpers
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
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
from work_ledger_projection_helpers import CONDUCTOR

from kiro_crew import work_ledger
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.handlers import remote_dispatch_routes as crew_routes
from kiro_crew.dashboard.handlers import remote_ingest
from kiro_crew.dashboard.handlers import work_ledger as routes

_isolated = helpers._isolated
recorded = hub.recorded


@pytest.fixture(autouse=True)
def _no_background_puller(_floor_monkeypatch):
    from kiro_crew.dashboard.handlers import remote_puller

    _floor_monkeypatch.setattr(remote_puller, "ensure_running", lambda state: None)


#: The verbs the stand-in gate was asked for, newest last.
GATED: list[str] = []


async def _admit(request: Any, verb: str) -> str:
    """An admitting stand-in for ``authorize_remote_dispatch``; its arms are pinned
    in ``test_session_control_crew``. Records the verb so a test sees it ran."""
    GATED.append(verb)
    return CONDUCTOR


@pytest.fixture
def peer(monkeypatch) -> StubCrew:
    monkeypatch.setattr(remote_ingest, "_CLOSE_RETRY", {})
    monkeypatch.setattr("kiro_crew.remote_dispatch.remote_dispatch_enabled", lambda: True)
    monkeypatch.setattr(crew_routes, "remote_dispatch_enabled", lambda: True)
    monkeypatch.setattr(crew_routes, "authorize_remote_dispatch", _admit)
    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)
    GATED.clear()
    return StubCrew()


async def _closes_done() -> None:
    if remote_ingest._CLOSE_TASKS:
        await asyncio.gather(*list(remote_ingest._CLOSE_TASKS))


async def _retire(state: Any, item_id: str, reason: str, sk: str = CONDUCTOR):
    app = web.Application()
    app["state"] = state
    req = make_mocked_request(
        "POST", "/api/work-ledger/record", app=app, headers={"X-Session-Key": sk}
    )
    req["internal_auth"] = True
    req.json = AsyncMock(  # type: ignore[method-assign]
        return_value={"action": "retire", "item_id": item_id, "reason": reason}
    )
    resp = await routes.api_work_ledger_record(req)
    return resp.status, json.loads(resp.text)


@pytest.mark.parametrize("reason", ["login_blocked", "wrong_crew"])
def test_a_conductor_retire_keeps_the_item_and_closes_the_old_slot(recorded, peer, reason):
    async def go() -> None:
        item_id = await _remote_item(peer)
        peer.report("blocked", "gh login", "2026-11-16T08:00:00+00:00", artifacts={"branch": "b1"})
        await _read(peer)
        state = _state(peer)
        crew_routes._record_dispatch(state, CONDUCTOR, CREW, PEER_SLOT)
        peer.calls.clear()

        status, body = await _retire(state, item_id, reason)
        assert status == 200, body
        assert GATED == ["retire"]
        await _closes_done()

        item = work_ledger.read_work_item(CONDUCTOR, item_id)
        assert item.state == "open" and item.summary == "gh login"
        assert item.worker_session_key is None and item.worker_instance_id is None
        (entry,) = item.retired_bindings
        assert entry["worker_instance_id"] == CREW and entry["branch"] == "b1"
        assert entry["reason"] == reason and entry["state"] == "destroyed"
        kinds = [e.kind for e in work_ledger.read_events(CONDUCTOR, item_id)]
        assert kinds[-2:] == ["retire", "destroy"]
        assert [c["path"] for c in peer.calls if c["method"] == "DELETE"] == [
            f"api/chat/slots/{PEER_SLOT}"
        ]
        assert not crew_routes.dispatched_by(state, CONDUCTOR, CREW, PEER_SLOT)
        row = _row(await _read(peer), item_id)
        assert row["redispatch_due"] is True and row["retired_crews"] == [CREW]

    asyncio.run(go())


def test_a_new_crew_binds_the_item_after_a_conductor_retire(recorded, peer):
    async def go() -> None:
        item_id = await _remote_item(peer)
        state = _state(peer)
        status, body = await _retire(state, item_id, "wrong_crew")
        assert status == 200, body
        await _closes_done()
        await remote_ingest.create_bound(
            state, peer, CONDUCTOR, "inst-other", item_id, {"memory_mode": "persistent"}
        )
        item = work_ledger.read_work_item(CONDUCTOR, item_id)
        assert item.worker_instance_id == "inst-other"
        assert remote_ingest.retired_crews(item) == {CREW}

    asyncio.run(go())


def test_only_the_owning_conductor_may_retire(recorded, peer):
    async def go() -> None:
        item_id = await _remote_item(peer)
        status, body = await _retire(_state(peer), item_id, "login_blocked", sk="chat-other-1")
        assert status == 404 and body["code"] == work_ledger.CODE_UNKNOWN_ITEM
        assert work_ledger.read_work_item(CONDUCTOR, item_id).worker_instance_id == CREW

    asyncio.run(go())


def test_a_local_bound_item_is_refused_retire_not_remote(recorded, peer):
    async def go() -> None:
        item_id = await helpers._board()
        status, body = await _retire(_state(peer), item_id, "login_blocked")
        assert status == 409 and body["code"] == remote_ingest.CODE_RETIRE_NOT_REMOTE
        item = work_ledger.read_work_item(CONDUCTOR, item_id)
        assert item.worker_session_key and item.retired_bindings == []

    asyncio.run(go())


def test_an_unbound_item_is_refused_retire_not_bound(recorded, peer):
    async def go() -> None:
        item_id = await _remote_item(peer)
        status, _ = await _retire(_state(peer), item_id, "login_blocked")
        assert status == 200
        status, body = await _retire(_state(peer), item_id, "login_blocked")
        assert status == 409 and body["code"] == remote_ingest.CODE_RETIRE_NOT_BOUND

    asyncio.run(go())


def test_the_hubs_own_reason_is_refused_by_the_schema(recorded, peer):
    async def go() -> None:
        item_id = await _remote_item(peer)
        status, body = await _record_via(
            peer, {"action": "retire", "item_id": item_id, "reason": "crew_unreachable"}
        )
        assert status == 400, body
        status, body = await _record_via(peer, {"action": "retire", "item_id": item_id})
        assert status == 400 and body["field"] == "reason"
        assert work_ledger.read_work_item(CONDUCTOR, item_id).worker_instance_id == CREW

    asyncio.run(go())


def test_switch_off_refuses_a_conductor_retire(recorded, peer, monkeypatch):
    async def go() -> None:
        item_id = await _remote_item(peer)
        monkeypatch.setattr(crew_routes, "remote_dispatch_enabled", lambda: False)
        peer.calls.clear()
        status, body = await _retire(_state(peer), item_id, "login_blocked")
        assert status == 403 and body["code"] == "remote_dispatch_disabled"
        assert work_ledger.read_work_item(CONDUCTOR, item_id).worker_instance_id == CREW
        assert peer.calls == []

    asyncio.run(go())


def test_a_pull_in_flight_during_a_retire_does_not_fence_the_item(recorded, peer, monkeypatch):
    async def go() -> None:
        item_id = await _remote_item(peer)
        state = _state(peer)
        real = remote_ingest._read_peer_item

        async def retire_mid_pull(*args: Any) -> Any:
            answer = await real(*args)
            status, body = await _retire(state, item_id, "login_blocked")
            assert status == 200, body
            return answer

        monkeypatch.setattr(remote_ingest, "_read_peer_item", retire_mid_pull)
        await remote_ingest.ingest_board(state, CONDUCTOR)
        record = work_ledger.read_remote_state(CONDUCTOR, item_id)
        assert record is None or record.binding is None
        monkeypatch.setattr(remote_ingest, "_read_peer_item", real)
        await remote_ingest.create_bound(
            state, peer, CONDUCTOR, "inst-other", item_id, {"memory_mode": "persistent"}
        )
        assert work_ledger.read_work_item(CONDUCTOR, item_id).worker_instance_id == "inst-other"

    asyncio.run(go())


def test_a_code_channel_conductor_gets_only_the_published_fields(recorded, peer):
    async def go() -> None:
        item_id = await _remote_item(peer)
        app = web.Application()
        app["state"] = _state(peer)
        req = make_mocked_request(
            "POST", "/api/work-ledger/record", app=app, headers={"X-Session-Key": CONDUCTOR}
        )
        req["internal_auth"] = True
        req["work_ledger.code_channel_caller"] = True
        req.json = AsyncMock(  # type: ignore[method-assign]
            return_value={"action": "retire", "item_id": item_id, "reason": "wrong_crew"}
        )
        resp = await routes.api_work_ledger_record(req)
        body = json.loads(resp.text)
        assert resp.status == 200, body
        assert body["published_fields_only"] is True
        assert "retired_bindings" not in body["item"] and "acceptance" not in body["item"]
        assert work_ledger.read_work_item(CONDUCTOR, item_id).retired_bindings

    asyncio.run(go())


def test_a_retire_on_a_stale_read_does_not_fence_the_item(recorded, peer):
    async def go() -> None:
        item_id = await _remote_item(peer)
        state = _state(peer)
        stale = work_ledger.read_work_item(CONDUCTOR, item_id)
        # Another retire lands between this caller's read and its write.
        status, body = await _retire(state, item_id, "wrong_crew")
        assert status == 200, body
        resp = await remote_ingest._record_retire(state, CONDUCTOR, stale, "login_blocked")
        assert resp is not None and resp.status == 409
        assert json.loads(resp.text)["code"] == remote_ingest.CODE_RETIRE_BINDING_MOVED
        record = work_ledger.read_remote_state(CONDUCTOR, item_id)
        assert record is None or record.binding is None
        await remote_ingest.create_bound(
            state, peer, CONDUCTOR, "inst-other", item_id, {"memory_mode": "persistent"}
        )
        assert work_ledger.read_work_item(CONDUCTOR, item_id).worker_instance_id == "inst-other"

    asyncio.run(go())


def test_a_turn_the_owner_did_not_send_is_refused_and_closes_nothing(recorded, peer, monkeypatch):
    """A wake or session_send turn: the owner arm refuses before any write or close."""

    async def refuse(request: Any, verb: str) -> str:
        raise crew_routes.CrewCallError(
            403, crew_routes.OWNER_UNVERIFIABLE, "crew= is open only to a turn the owner sent"
        )

    async def go() -> None:
        item_id = await _remote_item(peer)
        state = _state(peer)
        crew_routes._record_dispatch(state, CONDUCTOR, CREW, PEER_SLOT)
        monkeypatch.setattr(crew_routes, "authorize_remote_dispatch", refuse)
        peer.calls.clear()
        status, body = await _retire(state, item_id, "login_blocked")
        assert status == 403 and body["code"] == crew_routes.OWNER_UNVERIFIABLE
        assert "owner retire it from their own turn" in body["error"]
        await _closes_done()
        await remote_ingest.ingest_board(state, CONDUCTOR)
        await _closes_done()
        item = work_ledger.read_work_item(CONDUCTOR, item_id)
        assert item.worker_instance_id == CREW and item.worker_session_key == PEER_SLOT
        assert item.retired_bindings == []
        assert [c for c in peer.calls if c["method"] == "DELETE"] == []
        assert crew_routes.dispatched_by(state, CONDUCTOR, CREW, PEER_SLOT)

    asyncio.run(go())


def test_session_control_off_refuses_a_conductor_retire(recorded, peer, monkeypatch):
    async def go() -> None:
        item_id = await _remote_item(peer)
        monkeypatch.setattr(sc, "session_control_enabled", lambda: False)
        peer.calls.clear()
        status, body = await _retire(_state(peer), item_id, "wrong_crew")
        assert status == 403 and body["code"] == "session_control_disabled"
        assert work_ledger.read_work_item(CONDUCTOR, item_id).worker_instance_id == CREW
        assert peer.calls == [] and GATED == []

    asyncio.run(go())
