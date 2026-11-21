"""Reclaiming the worker a lost create reply leaves on a crew.

Pinned here:

* the peer's batch reply names, under ``held``, each live worker it holds for the
  hub conductor that the pull did not name, its copy open or closed, leaving out
  the slots the hub says it is already closing so repeated pulls reach them all;
* the hub binds that worker when its item is open and unbound;
* otherwise it sends it through the wake-turn close path: ``trust: false`` now,
  and a DELETE only once the crew lists the slot idle and its copy reported done;
* a worker an item already holds, or any work-item create still in flight, is
  left alone.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
import test_remote_dispatch_batch_pull as batch
import test_remote_dispatch_hub_ingest as hub
import work_ledger_projection_helpers as helpers
from test_remote_dispatch_hub_ingest import CREW, PEER_SLOT, _read, _remote_item, _state
from work_ledger_projection_helpers import ACCEPTANCE, CONDUCTOR, _record

from kiro_crew import work_ledger
from kiro_crew.dashboard.handlers import (
    remote_batch_pull,
    remote_ingest,
    remote_placement,
    remote_work_items_pull,
)

_isolated = helpers._isolated
recorded = hub.recorded
_quiet = batch._quiet

ORPHAN = "chat-90-1793000000"


class ReclaimCrew(batch.BatchCrew):
    """A batch crew that also answers ``held``: *held* as set, or, with *orphans*
    set, the peer route's own rule (leave out ``held_skip``, stop at *cap*)."""

    def __init__(self) -> None:
        super().__init__()
        self.held: dict[str, Any] = {}
        #: item id -> worker slot, answered by the route's rule.
        self.orphans: dict[str, str] = {}
        self.cap = 1
        self.skips: list[list[str]] = []

    def _rows(self, body: dict[str, Any]) -> dict[str, Any]:
        skip = list(body.get("held_skip", []))
        self.skips.append(skip)
        held = dict(self.held)
        for item_id, slot in self.orphans.items():
            if slot not in skip and len(held) < self.cap:
                held[item_id] = {"slot": slot}
        return {**super()._rows(body), "held": held}


@pytest.fixture
def peer(monkeypatch) -> ReclaimCrew:
    monkeypatch.setattr("kiro_crew.remote_dispatch.remote_dispatch_enabled", lambda: True)
    return ReclaimCrew()


async def _unbound_item(title: str = "lost reply") -> str:
    status, body = await _record(
        CONDUCTOR, {"action": "create", "title": title, "acceptance": ACCEPTANCE}
    )
    assert status == 200, body
    return str(body["item"]["item_id"])


async def _closes_done() -> None:
    if remote_ingest._CLOSE_TASKS:
        await asyncio.wait_for(asyncio.gather(*list(remote_ingest._CLOSE_TASKS)), 30)


def _orphan_calls(peer: ReclaimCrew, mark: int) -> list[tuple[str, str, Any]]:
    return [
        (c["method"], c["path"], c["body"])
        for c in peer.calls[mark:]
        if ORPHAN in c["path"] and c["method"] in ("PATCH", "DELETE")
    ]


def test_a_lost_create_reply_is_bound_on_the_next_pull(recorded, peer):
    async def go() -> tuple[str, list[tuple[str, str, Any]]]:
        await _remote_item(peer)
        item_id = await _unbound_item()
        # Decided while unbound: no peer copy was reached then.
        status, body = await _record(
            CONDUCTOR, {"action": "decide", "item_id": item_id, "decision": "use main"}
        )
        assert status == 200, body
        peer.held = {item_id: {"slot": ORPHAN}}
        mark = len(peer.calls)
        await _read(peer)
        return item_id, _orphan_calls(peer, mark)

    item_id, calls = asyncio.run(go())
    assert [(m, b) for m, _p, b in calls] == [("PATCH", {"decision": "use main"})]
    item = work_ledger.read_work_item(CONDUCTOR, item_id)
    assert (item.worker_instance_id, item.worker_session_key) == (CREW, ORPHAN)
    binds = [d for _u, d in recorded if d.get("action") == "bind" and d.get("item_id") == item_id]
    assert binds and binds[-1]["worker_session_key"] == ORPHAN


def test_a_running_orphan_loses_its_trust_and_is_not_deleted(recorded, peer):
    async def go() -> list[tuple[str, str, Any]]:
        await _remote_item(peer)
        # An item this hub never holds: the worker is nobody's.
        peer.held = {"it_0000abcd": {"slot": ORPHAN}}
        peer.slots.append({"key": ORPHAN, "running": True})
        mark = len(peer.calls)
        await _read(peer)
        await _closes_done()
        return _orphan_calls(peer, mark)

    calls = asyncio.run(go())
    assert calls == [("PATCH", f"api/chat/slots/{ORPHAN}/work-item", {"trust": False})]


def test_an_idle_done_orphan_of_a_bound_item_is_deleted(recorded, peer):
    async def go() -> list[tuple[str, str, Any]]:
        item_id = await _remote_item(peer)
        # The item is bound to another worker on this crew; the orphan is a
        # second create's whose reply was lost.
        peer.held = {item_id: {"slot": ORPHAN}}
        peer.slots.append({"key": ORPHAN, "running": False})
        peer.work_item["status"] = "done"
        mark = len(peer.calls)
        await _read(peer)
        await _closes_done()
        return _orphan_calls(peer, mark)

    calls = asyncio.run(go())
    assert [c[0] for c in calls] == ["PATCH", "DELETE"]
    assert calls[1][1] == f"api/chat/slots/{ORPHAN}"
    # No item holds it, so the DELETE alone ends the close: nothing left to skip.
    assert (CREW, ORPHAN) not in remote_ingest._CLOSE_RETRY
    assert remote_batch_pull.held_skip(CREW, CONDUCTOR) == []


def test_a_worker_an_item_already_holds_is_left_alone(recorded, peer):
    async def go() -> list[tuple[str, str]]:
        item_id = await _remote_item(peer)
        # Named as held (as an item past the per-read cap would be), but bound here.
        peer.held = {item_id: {"slot": PEER_SLOT}}
        mark = len(peer.calls)
        await _read(peer)
        await _closes_done()
        return [(c["method"], c["path"]) for c in peer.calls[mark:]]

    calls = asyncio.run(go())
    assert all(method not in ("PATCH", "DELETE") for method, _path in calls)


def test_nothing_is_reclaimed_while_a_create_is_in_flight(recorded, peer):
    async def go() -> str:
        await _remote_item(peer)
        item_id = await _unbound_item()
        peer.held = {item_id: {"slot": ORPHAN}}
        state = _state(peer)
        async with remote_placement.holding(state, "inst-other"):
            await remote_ingest.ingest_board(state, CONDUCTOR)
        return item_id

    item_id = asyncio.run(go())
    assert not work_ledger.read_work_item(CONDUCTOR, item_id).worker_session_key


def test_the_hub_keeps_only_usable_held_rows():
    batch_reply = remote_batch_pull._parse(
        {
            "items": {},
            "held": {
                "it_0000aaaa": {"slot": ORPHAN},
                "not-an-id": {"slot": ORPHAN},
                "it_0000bbbb": {"slot": "../x"},
                "it_0000cccc": "junk",
            },
        },
        set(),
    )
    assert batch_reply is not None
    assert batch_reply.held == {"it_0000aaaa": {"slot": ORPHAN, "closed": False}}
    assert batch_reply.held_omitted == 0
    closed = remote_batch_pull._parse(
        {"items": {}, "held": {"it_0000aaaa": {"slot": ORPHAN, "closed": True}}}, set()
    )
    assert closed is not None and closed.held["it_0000aaaa"]["closed"] is True
    older = remote_batch_pull._parse({"items": {}}, set())
    assert older is not None and older.held == {} and older.held_omitted == 0


def test_an_oversized_held_reply_is_cut_at_the_cap_and_counted():
    cap = remote_batch_pull.MAX_HELD_ITEMS
    rows = {f"it_{n:08x}": {"slot": f"chat-{n}-1793000000"} for n in range(cap + 3)}
    batch_reply = remote_batch_pull._parse({"items": {}, "held": rows, "held_omitted": 2}, set())
    assert batch_reply is not None and len(batch_reply.held) == cap
    # The peer's own overflow count plus the rows past this hub's cap.
    assert batch_reply.held_omitted == 2 + 3
    for junk in (True, -1, "9", 2**40):
        odd = remote_batch_pull._parse({"items": {}, "held": {}, "held_omitted": junk}, set())
        assert odd is not None and odd.held_omitted == 0


def test_a_create_that_starts_mid_reclaim_holds_off_the_rest(recorded, peer, monkeypatch):
    """Each worker is decided under the create hold, not once per pull: a create
    that starts after the first one is settled keeps the second from binding."""
    from kiro_crew.dashboard.handlers import remote_reclaim

    real = remote_reclaim._schedule_close
    started: list[Any] = []

    def close_then_create(state, mgr, board, crew, slot):
        real(state, mgr, board, crew, slot)
        # A create that took the hold right after this decision, as ``holding``
        # does: counted in flight, its reply not back yet.
        remote_placement._INFLIGHT.setdefault(state, {})["inst-b"] = 1
        started.append(state)

    monkeypatch.setattr(remote_reclaim, "_schedule_close", close_then_create)

    async def go() -> str:
        await _remote_item(peer)
        item_id = await _unbound_item()
        peer.held = {"it_0000abcd": {"slot": "chat-91-1793000000"}, item_id: {"slot": ORPHAN}}
        state = _state(peer)
        try:
            await remote_ingest.ingest_board(state, CONDUCTOR)
        finally:
            remote_placement._INFLIGHT.pop(state, None)
        await _closes_done()
        return item_id

    item_id = asyncio.run(go())
    assert started
    assert not work_ledger.read_work_item(CONDUCTOR, item_id).worker_session_key


def test_a_bind_that_lands_mid_reclaim_is_read_before_deciding(recorded, peer, monkeypatch):
    """The item is read settled when its worker is decided: a bind that lands after
    the pull and the first decision leaves that worker alone, never revoked."""
    from kiro_crew.dashboard.handlers import remote_reclaim

    real = remote_reclaim._schedule_close
    item_ids: list[str] = []

    def close_then_bind(state, mgr, board, crew, slot):
        real(state, mgr, board, crew, slot)
        work_ledger.apply_remote_bind(
            CONDUCTOR, item_ids[0], worker_session_key=ORPHAN, worker_instance_id=CREW
        )

    monkeypatch.setattr(remote_reclaim, "_schedule_close", close_then_bind)

    async def go() -> list[tuple[str, str, Any]]:
        await _remote_item(peer)
        item_ids.append(await _unbound_item())
        peer.held = {"it_0000abcd": {"slot": "chat-91-1793000000"}, item_ids[0]: {"slot": ORPHAN}}
        peer.slots.append({"key": ORPHAN, "running": False})
        peer.work_item["status"] = "done"
        mark = len(peer.calls)
        await _read(peer)
        await _closes_done()
        return _orphan_calls(peer, mark)

    calls = asyncio.run(go())
    assert calls == []
    item = work_ledger.read_work_item(CONDUCTOR, item_ids[0])
    assert (item.worker_instance_id, item.worker_session_key) == (CREW, ORPHAN)


def test_the_peer_route_names_each_live_unnamed_worker(monkeypatch):
    from kiro_crew import remote_dispatch_mailbox as mailbox

    hub_key = "chat-hub-conductor"
    for item_id, slot in (
        ("it_0000aaaa", "chat-1-1793000000"),
        ("it_0000bbbb", "chat-2-1793000000"),
        ("it_0000cccc", "chat-3-1793000000"),
    ):
        mailbox.open_mailbox(
            mailbox.parse_work_item(
                {
                    "conductor_key": hub_key,
                    "item_id": item_id,
                    "title": "a",
                    "acceptance": ACCEPTANCE,
                }
            ),
            slot,
        )
    board = mailbox.mailbox_key(hub_key)
    # A closed copy whose worker slot is still live is listed too, marked closed.
    mailbox.open_mailbox(
        mailbox.parse_work_item(
            {
                "conductor_key": hub_key,
                "item_id": "it_0000dddd",
                "title": "d",
                "acceptance": ACCEPTANCE,
            }
        ),
        "chat-4-1793000000",
    )
    work_ledger.apply_conductor_action(
        board, "close", item_id="it_0000dddd", state="abandoned", decision="gone"
    )
    # The third worker's slot is gone here: it holds nothing to reclaim.
    state = SimpleNamespace(
        _slots={
            "chat-1-1793000000": object(),
            "chat-2-1793000000": object(),
            "chat-4-1793000000": object(),
        }
    )
    named = [("it_0000aaaa", "chat-1-1793000000", (), None, None)]
    unnamed = remote_work_items_pull._unnamed(board, named)
    assert sorted(unnamed) == [
        ("it_0000bbbb", "chat-2-1793000000", False),
        ("it_0000cccc", "chat-3-1793000000", False),
        ("it_0000dddd", "chat-4-1793000000", True),
    ]
    held = remote_work_items_pull._held(state, unnamed)
    assert held == (
        {
            "it_0000bbbb": {"slot": "chat-2-1793000000"},
            "it_0000dddd": {"slot": "chat-4-1793000000", "closed": True},
        },
        0,
    )
    # A slot the hub is already closing is left out, so the cap reaches the next.
    skip = remote_work_items_pull._skip(["chat-2-1793000000"])
    assert skip is not None
    assert remote_work_items_pull._held(state, unnamed, skip)[0] == {
        "it_0000dddd": {"slot": "chat-4-1793000000", "closed": True}
    }
    assert remote_work_items_pull._skip("chat-2") is None
    assert remote_work_items_pull._skip([""]) is None
    assert remote_work_items_pull._skip(["s"] * (remote_work_items_pull.MAX_HELD_SKIP + 1)) is None
    # Past the cap the rest is counted, not dropped silently; a gone slot is neither.
    monkeypatch.setattr(remote_work_items_pull, "MAX_HELD_ITEMS", 0)
    assert remote_work_items_pull._held(state, unnamed) == ({}, 2)


def test_a_closed_crew_copy_with_a_live_worker_is_closed_not_bound(recorded, peer):
    async def go() -> tuple[str, list[tuple[str, str, Any]]]:
        await _remote_item(peer)
        item_id = await _unbound_item()
        peer.held = {item_id: {"slot": ORPHAN, "closed": True}}
        peer.slots.append({"key": ORPHAN, "running": True})
        mark = len(peer.calls)
        await _read(peer)
        await _closes_done()
        return item_id, _orphan_calls(peer, mark)

    item_id, calls = asyncio.run(go())
    assert calls == [("PATCH", f"api/chat/slots/{ORPHAN}/work-item", {"trust": False})]
    assert not work_ledger.read_work_item(CONDUCTOR, item_id).worker_session_key


def test_repeated_pulls_reach_every_running_orphan_past_the_cap(recorded, peer):
    """A running orphan keeps its slot after its Trust is revoked; the slots the
    hub is already closing ride as ``held_skip``, so each pull reaches new ones."""
    slots = [f"chat-{n}-1793000000" for n in (91, 92, 93)]

    async def go() -> list[str]:
        await _remote_item(peer)
        peer.orphans = {f"it_0000a{n}00": slot for n, slot in enumerate(slots)}
        peer.slots.extend({"key": slot, "running": True} for slot in slots)
        for _ in slots:
            await _read(peer)
            await _closes_done()
        return [
            c["path"]
            for c in peer.calls
            if c["method"] == "PATCH" and c["body"] == {"trust": False}
        ]

    revoked = asyncio.run(go())
    assert sorted(revoked) == sorted(f"api/chat/slots/{s}/work-item" for s in slots)
    assert peer.skips[-1] == sorted(slots[:2])
    # None of them was deleted: each is still running.
    assert not [c for c in peer.calls if c["method"] == "DELETE"]


def test_held_skip_names_only_this_boards_closes_and_is_never_cut(monkeypatch):
    from kiro_crew.dashboard.state_parts.slot_directory import MAX_LIVE_SLOTS

    # A crew never holds more live slots than the skip list may name.
    assert remote_work_items_pull.MAX_HELD_SKIP >= MAX_LIVE_SLOTS
    mine = [f"chat-{n}-1793000000" for n in range(MAX_LIVE_SLOTS)]
    retry = {(CREW, slot): (1, float("inf")) for slot in [*mine, "chat-other-1793000000"]}
    owners = {(CREW, slot): CONDUCTOR for slot in mine}
    owners[(CREW, "chat-other-1793000000")] = "chat-another-board"
    retry[("inst-elsewhere", "chat-x-1793000000")] = (1, float("inf"))
    owners[("inst-elsewhere", "chat-x-1793000000")] = CONDUCTOR
    monkeypatch.setattr(remote_ingest, "_CLOSE_RETRY", retry)
    monkeypatch.setattr(remote_batch_pull, "CLOSE_BOARD", owners)
    assert remote_batch_pull.held_skip(CREW, CONDUCTOR) == sorted(mine)
    # The peer takes a full list.
    assert remote_work_items_pull._skip(sorted(mine)) == frozenset(mine)


def test_reclaim_runs_before_the_per_item_fallback(monkeypatch):
    """A crew that answered its batch has its held workers settled before any
    per-item read of a crew that did not, which may spend the whole deadline."""
    from kiro_crew.dashboard.handlers import remote_reclaim

    order: list[Any] = []
    held = {CREW: {"it_0000cccc": {"slot": ORPHAN, "closed": False}}}

    async def pull_batches(state, mgr, board, remote, deadline):
        return {"it_0000aaaa": {"more": False}}, held

    async def batched(state, mgr, board, item, row):
        order.append(("batched", item.item_id))

    async def per_item(state, mgr, board, item, slot_lists):
        order.append(("fallback", item.item_id))

    async def reclaim(state, mgr, board, got, deadline):
        order.append(("reclaim", got))

    monkeypatch.setattr(remote_batch_pull, "pull_batches", pull_batches)
    monkeypatch.setattr(remote_batch_pull, "ingest_batched", batched)
    monkeypatch.setattr(remote_ingest, "_ingest_item", per_item)
    monkeypatch.setattr(remote_reclaim, "reclaim", reclaim)
    items = [SimpleNamespace(item_id="it_0000bbbb"), SimpleNamespace(item_id="it_0000aaaa")]
    state = SimpleNamespace(instances_manager=object())
    asyncio.run(remote_ingest._pull_all(state, CONDUCTOR, items))  # type: ignore[arg-type]
    assert order == [
        ("batched", "it_0000aaaa"),
        ("reclaim", held),
        ("fallback", "it_0000bbbb"),
    ]
