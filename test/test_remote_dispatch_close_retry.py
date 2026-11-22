"""A retired peer slot whose close fails is retried by a later sweep, not forgotten.

Drives the close sweep (``remote_puller.sweep_closes``) against the wave 4 stub
crew. Pinned here:

* a DELETE that times out leaves the entry ``retired`` and ``close_pending == 1``;
  a sweep inside the backoff sends nothing; the first sweep past it closes the
  slot, the entry reads ``destroyed`` and ``close_pending == 0``;
* every try sends ``PATCH trust: false`` before its DELETE;
* a 404 ``slot_not_found`` counts as closed on the first sweep;
* a crew that is not connected costs no try and starts no backoff;
* a hub restart mid-backoff (the in-memory backoff is gone) tries at once.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

import pytest
import test_remote_dispatch_hub_ingest as hub
import work_ledger_projection_helpers as helpers
from test_remote_dispatch_hub_ingest import (
    CREW,
    PEER_SLOT,
    StubCrew,
    _read,
    _remote_item,
    _row,
    _state,
)
from test_remote_dispatch_retire import _closes_done, _no_background_puller, _retire_on, clock
from work_ledger_projection_helpers import CONDUCTOR

from kiro_crew import work_ledger
from kiro_crew.dashboard.handlers import remote_ingest, remote_puller

_isolated = helpers._isolated
recorded = hub.recorded
__all__ = ["_isolated", "_no_background_puller", "clock", "recorded"]

_KEY = (CREW, PEER_SLOT)
_DELETE = f"api/chat/slots/{PEER_SLOT}"
_PATCH = f"api/chat/slots/{PEER_SLOT}/work-item"


class _StallingCrew(StubCrew):
    """The stub crew, with the first *stalls* DELETEs timing out.

    The DELETE raises the ``TimeoutError`` the hub's own ``wait_for`` raises when a
    peer stalls, so no deadline is shortened and no wall clock is waited on.
    """

    def __init__(self) -> None:
        super().__init__()
        self.stalls = 0

    @asynccontextmanager
    async def proxy_request(self, instance_id, method, path, **kwargs):
        if method == "DELETE" and self.state == "connected" and self.stalls:
            self.stalls -= 1
            self.calls.append({"method": method, "path": path, "params": None, "body": None})
            raise TimeoutError
        async with super().proxy_request(instance_id, method, path, **kwargs) as resp:
            yield resp


@pytest.fixture
def peer(monkeypatch, clock) -> _StallingCrew:
    stub = _StallingCrew()
    stub.clock = clock  # type: ignore[attr-defined]
    monkeypatch.setattr("kiro_crew.remote_dispatch.remote_dispatch_enabled", lambda: True)
    return stub


def _entry(item_id: str) -> dict[str, Any]:
    (entry,) = work_ledger.read_work_item(CONDUCTOR, item_id).retired_bindings
    return entry


async def _close_pending(peer: StubCrew, item_id: str) -> int:
    """The compact read row's ``close_pending``, through the work-ledger route."""
    return _row(await _read(peer, "compact=true"), item_id)["close_pending"]


def _close_calls(peer: StubCrew) -> list[tuple[str, str, Any]]:
    return [
        (c["method"], c["path"], c["body"])
        for c in peer.calls
        if c["method"] in ("PATCH", "DELETE")
    ]


async def _sweep(state: Any) -> None:
    await remote_puller.sweep_closes(state)
    await _closes_done()


async def _retired(peer: StubCrew) -> tuple[Any, str]:
    item_id = await _remote_item(peer)
    state = _state(peer)
    await _retire_on(state, peer, item_id)
    peer.calls.clear()
    return state, item_id


def test_a_timed_out_close_is_retried_after_its_backoff_and_trust_goes_first(recorded, peer):
    async def go() -> None:
        state, item_id = await _retired(peer)
        assert await _close_pending(peer, item_id) == 1
        peer.state = "connected"
        peer.stalls = 1
        # Sweep one: the trust PATCH lands, the DELETE times out.
        await _sweep(state)
        assert _close_calls(peer) == [
            ("PATCH", _PATCH, {"trust": False}),
            ("DELETE", _DELETE, None),
        ]
        assert _entry(item_id)["state"] == "retired"
        assert await _close_pending(peer, item_id) == 1
        assert remote_ingest._CLOSE_RETRY[_KEY][0] == 1
        # A sweep inside the backoff sends nothing.
        peer.calls.clear()
        peer.clock.now += remote_ingest.CLOSE_RETRY_SECS - 1  # type: ignore[attr-defined]
        await _sweep(state)
        assert _close_calls(peer) == []
        # Sweep two, past the backoff: PATCH again, then a DELETE that answers 200.
        peer.clock.now += 1  # type: ignore[attr-defined]
        await _sweep(state)
        assert _close_calls(peer) == [
            ("PATCH", _PATCH, {"trust": False}),
            ("DELETE", _DELETE, None),
        ]
        assert _entry(item_id)["state"] == "destroyed"
        assert await _close_pending(peer, item_id) == 0
        assert _KEY not in remote_ingest._CLOSE_RETRY
        # Closed: no later sweep calls the peer for it.
        peer.calls.clear()
        peer.clock.now += remote_ingest.CLOSE_RETRY_MAX_SECS  # type: ignore[attr-defined]
        await _sweep(state)
        assert _close_calls(peer) == []

    asyncio.run(go())


def test_a_slot_the_peer_no_longer_holds_is_destroyed_on_the_first_sweep(recorded, peer):
    async def go() -> None:
        state, item_id = await _retired(peer)
        peer.state = "connected"
        real = peer._answer
        peer._answer = lambda m, p, b: (  # type: ignore[method-assign]
            (404, {"code": "slot_not_found"}) if m == "DELETE" else real(m, p, b)
        )
        await _sweep(state)
        assert [m for m, _p, _b in _close_calls(peer)] == ["PATCH", "DELETE"]
        assert _entry(item_id)["state"] == "destroyed"
        assert await _close_pending(peer, item_id) == 0
        assert _KEY not in remote_ingest._CLOSE_RETRY

    asyncio.run(go())


def test_a_crew_that_is_not_connected_costs_no_try(recorded, peer):
    async def go() -> None:
        state, item_id = await _retired(peer)
        peer.state = "disconnected"
        await _sweep(state)
        assert _close_calls(peer) == []
        assert _KEY not in remote_ingest._CLOSE_RETRY
        # Back: the very next sweep closes it, with no backoff to wait out.
        peer.state = "connected"
        await _sweep(state)
        assert _entry(item_id)["state"] == "destroyed"

    asyncio.run(go())


def test_a_hub_restart_inside_the_backoff_tries_at_once(recorded, peer):
    async def go() -> None:
        state, item_id = await _retired(peer)
        peer.state = "connected"
        peer.stalls = 1
        await _sweep(state)
        assert _entry(item_id)["state"] == "retired"
        # A new process starts with no backoff in memory.
        remote_ingest._CLOSE_RETRY.clear()
        peer.calls.clear()
        await _sweep(state)
        assert [m for m, _p, _b in _close_calls(peer)] == ["PATCH", "DELETE"]
        assert _entry(item_id)["state"] == "destroyed"

    asyncio.run(go())


def test_the_backoff_doubles_and_stops_at_its_cap(recorded, peer):
    async def go() -> None:
        state, _item_id = await _retired(peer)
        peer.state = "connected"
        real = peer._answer
        peer._answer = lambda m, p, b: (  # type: ignore[method-assign]
            (500, {"code": "boom"}) if m == "DELETE" else real(m, p, b)
        )
        waits = []
        for _ in range(9):
            await _sweep(state)
            failures, not_before = remote_ingest._CLOSE_RETRY[_KEY]
            waits.append(not_before - peer.clock.now)  # type: ignore[attr-defined]
            peer.clock.now = not_before  # type: ignore[attr-defined]
        base = remote_ingest.CLOSE_RETRY_SECS
        assert failures == 9
        assert waits[:6] == [base * 2**n for n in range(6)]
        assert waits[-1] == remote_ingest.CLOSE_RETRY_MAX_SECS

    asyncio.run(go())
