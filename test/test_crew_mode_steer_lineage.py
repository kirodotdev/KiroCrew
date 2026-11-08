"""A person's steer is judged by the lineage the slot frame reads, and recorded once
for a message that reached the crew member.

The composer send (``chat_send._person_crew_worker_root``) reads the session tree's
edges over the birth stamp, so a released session is a plain session again. A
stopped send records nothing, a steer queued behind an earlier entry is recorded
when it is delivered, and the item a steer lands on is the member's open one, on a
closed sub-conductor's ledger when that is where it lives.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
import work_ledger_helpers
from work_ledger_helpers import CONDUCTOR, WORKER

from kiro_crew import crew_mode
from kiro_crew import work_ledger as wl

_isolated_home = work_ledger_helpers._isolated_home

SUB = "chat-3-subconductor"
GRANDCHILD = "chat-4-grandchild"


@pytest.fixture(autouse=True)
def _routing_live(_floor_monkeypatch):
    # The routing is off on main until the Crew panel turns it on; these tests pin
    # the composer steer as it runs then.
    from kiro_crew import crew_mode

    _floor_monkeypatch.setattr(crew_mode, "APPROVAL_ROUTING_LIVE", True)


def _live(key: str, agent: str, created_by: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        key=key,
        agent=agent,
        agent_kind="template",
        crew_mode_agent="",
        mode="",
        memory_mode="persistent",
        _created_by=created_by,
        _saved_created_by="",
        _lineage_minted=bool(created_by),
        _app="",
        instance_id="",
    )


class _Req(dict):
    def __init__(self, state: Any) -> None:
        super().__init__()
        self.app = {"state": state}


# ── the composer reads the frame's lineage ───────────────────────────────────


def test_a_released_session_typed_into_is_a_plain_send(monkeypatch):
    from kiro_crew.dashboard.chat_api import chat_send
    from kiro_crew.dashboard.handlers import source_providers
    from kiro_crew.dashboard.session_control_parts import crew_filing

    monkeypatch.setattr(source_providers, "is_owner_dashboard_request", lambda r: True)
    root = _live("r", "kirocrew-conductor")
    worker = _live("w", "kirocrew-worker", created_by="r")
    state = SimpleNamespace(_slots={"r": root, "w": worker})
    # Born under the conductor: a steer while the edge stands.
    monkeypatch.setattr(crew_mode, "lineage_is_settled", lambda: False)
    monkeypatch.setattr(crew_filing, "_frame_parents", lambda s: None)
    assert chat_send._person_crew_worker_root(object(), state, worker, "") == "r"  # type: ignore[arg-type]
    # Released: the settled tree has no edge, so the birth stamp does not count.
    monkeypatch.setattr(crew_mode, "lineage_is_settled", lambda: True)
    monkeypatch.setattr(crew_mode, "persisted_tree_slots", lambda: frozenset({"r", "w"}))
    monkeypatch.setattr(crew_mode, "persisted_parents", lambda: {})
    monkeypatch.setattr(crew_filing, "_frame_parents", lambda s: {"r": None, "w": None})
    assert chat_send._person_crew_worker_root(object(), state, worker, "") == ""  # type: ignore[arg-type]


def test_with_the_routing_off_a_crew_members_composer_is_a_plain_send(monkeypatch):
    """On main the flag is off: typing into a crew member's own chat records no
    steer and refuses nothing, as it does today."""
    from kiro_crew.dashboard.chat_api import chat_send
    from kiro_crew.dashboard.handlers import source_providers
    from kiro_crew.dashboard.session_control_parts import crew_filing

    monkeypatch.setattr(crew_mode, "APPROVAL_ROUTING_LIVE", False)
    monkeypatch.setattr(source_providers, "is_owner_dashboard_request", lambda r: True)
    monkeypatch.setattr(crew_mode, "lineage_is_settled", lambda: False)
    monkeypatch.setattr(crew_filing, "_frame_parents", lambda s: None)
    root = _live("r", "kirocrew-conductor")
    worker = _live("w", "kirocrew-worker", created_by="r")
    state = SimpleNamespace(_slots={"r": root, "w": worker})
    assert chat_send._person_crew_worker_root(object(), state, worker, "") == ""  # type: ignore[arg-type]


# ── a send that never reached the member records nothing ─────────────────────


@pytest.mark.asyncio
async def test_a_stopped_composer_send_records_no_steer(monkeypatch):
    from aiohttp import web

    from kiro_crew.dashboard import chat_handlers
    from kiro_crew.dashboard.chat_api import chat_send
    from kiro_crew.dashboard.handlers import crew_workers

    async def fake_send(request):
        request[chat_send._CREW_PERSON_STEER] = (CONDUCTOR, WORKER, "x")
        return web.json_response({"ok": True, "slot": WORKER, "stopped": True})

    async def boom(*_a, **_k):
        raise AssertionError("a stopped send must not be recorded")

    monkeypatch.setattr(chat_handlers, "_api_chat_send", fake_send)
    monkeypatch.setattr(crew_workers, "person_steer", boom)
    resp = await chat_handlers.api_chat(_Req(SimpleNamespace()))  # type: ignore[arg-type]
    assert json.loads(resp.text) == {"ok": True, "slot": WORKER, "stopped": True}


@pytest.mark.asyncio
async def test_a_steer_behind_an_earlier_entry_is_reported_queued(monkeypatch):
    from kiro_crew.dashboard import chat_delivery, chat_runner
    from kiro_crew.dashboard.handlers import crew_workers

    queue: list[dict[str, Any]] = [{"id": "q1", "content": "earlier"}]

    def queue_for_next_turn(state, slot, message, **_k):
        queue.append({"id": "q2", "content": message})
        return "q2"

    async def drain_one(state, slot, **_k):
        # The drain takes the EARLIER entry; the person's stays queued.
        queue.pop(0)
        return True

    monkeypatch.setattr(chat_delivery, "queue_for_next_turn", queue_for_next_turn)
    monkeypatch.setattr(chat_runner, "_start_next_queued_turn", drain_one)
    slot = SimpleNamespace(key=WORKER, turn_running=False, running=False, _queue=queue)
    state = SimpleNamespace(push_slots_update=lambda: None)
    out = await crew_workers._deliver_as_person(state, slot, "use main")
    assert (out["queued"], out["started"], out["queue_id"]) == (True, False, "q2")


# ── the item a steer lands on ────────────────────────────────────────────────


def _bound(board: str, worker: str) -> str:
    wl.ensure_conductor(board, goal="g")
    item_id = wl.apply_conductor_action(
        board, "create", title="t", acceptance={"kind": "human_approval"}
    )["item"].item_id
    wl.apply_conductor_action(board, "bind", item_id=item_id, worker_session_key=worker)
    return item_id


def test_a_closed_sub_conductors_ledger_is_still_followed(monkeypatch):
    from kiro_crew.dashboard.handlers import crew_workers

    item_id = _bound(SUB, GRANDCHILD)
    # SUB's tab closed: it is not in the live tree, but the session tree records it
    # under the root.
    monkeypatch.setattr(crew_mode, "persisted_parents", lambda: {SUB: CONDUCTOR})
    assert crew_workers._worker_item(CONDUCTOR, GRANDCHILD, frozenset()) == (SUB, item_id)
    # A board the persisted lineage puts under another root is still refused.
    monkeypatch.setattr(crew_mode, "persisted_parents", lambda: {SUB: "chat-9-elsewhere"})
    assert crew_workers._worker_item(CONDUCTOR, GRANDCHILD, frozenset()) == ("", "")


def test_a_cycle_in_the_persisted_lineage_ends_the_walk(monkeypatch):
    from kiro_crew.dashboard.handlers import crew_workers

    monkeypatch.setattr(crew_mode, "persisted_parents", lambda: {"a": "b", "b": "a"})
    assert crew_workers._persisted_under("a", CONDUCTOR) is False


def test_an_open_item_wins_over_a_closed_one_for_the_same_member():
    from kiro_crew.dashboard.handlers import crew_workers

    wl.ensure_conductor(CONDUCTOR, goal="g")
    ids = []
    for _ in range(2):
        item_id = wl.apply_conductor_action(
            CONDUCTOR, "create", title="t", acceptance={"kind": "human_approval"}
        )["item"].item_id
        ids.append(item_id)
    wl.apply_conductor_action(CONDUCTOR, "bind", item_id=ids[1], worker_session_key=WORKER)
    wl.apply_conductor_action(CONDUCTOR, "close", item_id=ids[1], state="accepted")
    wl.apply_conductor_action(CONDUCTOR, "bind", item_id=ids[0], worker_session_key=WORKER)
    items = crew_workers._ledger_items(CONDUCTOR)
    assert items[WORKER].item == ids[0]


def test_a_grandchilds_item_is_read_from_the_sub_conductor_that_bound_it():
    from kiro_crew.dashboard.handlers import crew_workers

    sub, grandchild = "chat-3-subconductor", "chat-4-grandchild"
    _bound(CONDUCTOR, sub)
    wl.ensure_conductor(sub, goal="g2")
    item_id = wl.apply_conductor_action(
        sub, "create", title="nested", acceptance={"kind": "human_approval"}
    )["item"].item_id
    wl.apply_conductor_action(sub, "bind", item_id=item_id, worker_session_key=grandchild)
    wl.apply_conductor_action(sub, "close", item_id=item_id, state="accepted")
    # The root's own ledger never names the grandchild.
    assert grandchild not in crew_workers._ledger_items(CONDUCTOR)
    items = crew_workers._crew_ledger_items(CONDUCTOR, [sub, grandchild])
    assert items[grandchild].item == item_id
    assert items[grandchild].progress == crew_workers.GROUP_DONE
    # The sub-conductor itself is still the root's own item.
    assert items[sub].item == crew_workers._ledger_items(CONDUCTOR)[sub].item


# ── a steer in a running turn is recorded when the turn reads it ─────────────


@pytest.fixture
def _held(monkeypatch):
    from kiro_crew.dashboard.chat_api import chat_send
    from kiro_crew.dashboard.handlers import crew_workers

    calls: list[tuple[str, str, str]] = []

    async def fake_steer(state, root, worker, message, **_k):
        calls.append((root, worker, message))
        return {"ledger_event": "it_1", "ledger_error": None}

    monkeypatch.setattr(crew_workers, "person_steer", fake_steer)
    monkeypatch.setattr(chat_send, "_HELD_CREW_STEERS", {})
    return SimpleNamespace(cs=chat_send, calls=calls)


@pytest.mark.asyncio
async def test_a_held_steer_is_recorded_when_the_turn_reads_it(_held):
    import asyncio

    slot = SimpleNamespace(key=WORKER)
    state = SimpleNamespace(_background_tasks=set())
    _held.cs.hold_crew_steer(WORKER, "use main", CONDUCTOR, WORKER)
    _held.cs.crew_steer_consumed(state, slot, "use main")
    await asyncio.gather(*state._background_tasks)
    assert _held.calls == [(CONDUCTOR, WORKER, "use main")]
    # Read once, recorded once.
    _held.cs.crew_steer_consumed(state, slot, "use main")
    assert not state._background_tasks and len(_held.calls) == 1


def test_a_taken_back_steer_is_never_recorded(_held):
    slot = SimpleNamespace(key=WORKER)
    state = SimpleNamespace(_background_tasks=set())
    _held.cs.hold_crew_steer(WORKER, "oops", CONDUCTOR, WORKER)
    _held.cs.crew_steer_discarded(slot, "oops")
    _held.cs.crew_steer_consumed(state, slot, "oops")
    assert _held.calls == [] and _held.cs._HELD_CREW_STEERS == {}


def test_a_requeued_steer_moves_onto_its_queue_entry(_held):
    from kiro_crew.dashboard.chat_api.chat_send import CREW_STEER_QUEUE_KEY

    slot = SimpleNamespace(key=WORKER)
    entry: dict[str, Any] = {"id": "q1", "content": "later"}
    _held.cs.hold_crew_steer(WORKER, "later", CONDUCTOR, WORKER)
    _held.cs.crew_steer_requeued(slot, "later", entry)
    assert entry[CREW_STEER_QUEUE_KEY] == (CONDUCTOR, WORKER)
    assert _held.cs._HELD_CREW_STEERS == {}


@pytest.mark.asyncio
async def test_a_refused_duplicate_steer_keeps_the_first_ones_hold(_held, monkeypatch):
    import asyncio

    from kiro_crew.dashboard import chat_delivery
    from kiro_crew.dashboard.handlers import crew_workers

    outcomes = iter([chat_delivery.STEER_STEERED, "unavailable"])

    async def steer(state, slot, message, **_k):
        return next(outcomes)

    def queue_for_next_turn(state, slot, message, **_k):
        return "q1"

    monkeypatch.setattr(chat_delivery, "steer_into_running_turn", steer)
    monkeypatch.setattr(chat_delivery, "queue_for_next_turn", queue_for_next_turn)
    slot = SimpleNamespace(key=WORKER, turn_running=True, running=True, _queue=[])
    state = SimpleNamespace(push_slots_update=lambda: None, _background_tasks=set())
    from kiro_crew.dashboard import session_control

    monkeypatch.setattr(session_control, "containment_meta", lambda *_a: {})
    first = await crew_workers._deliver_as_person(
        state, slot, "same", root=CONDUCTOR, worker=WORKER
    )
    assert first["steered"] is True
    # The same text again before the turn reads the first: refused, and the
    # first steer's hold must survive the second attempt's clean-up.
    await crew_workers._deliver_as_person(state, slot, "same", root=CONDUCTOR, worker=WORKER)
    _held.cs.crew_steer_consumed(state, slot, "same")
    await asyncio.gather(*state._background_tasks)
    assert _held.calls == [(CONDUCTOR, WORKER, "same")]


def test_a_steer_nobody_held_is_left_alone(_held):
    entry: dict[str, Any] = {"id": "q1", "content": "x"}
    _held.cs.crew_steer_requeued(SimpleNamespace(key=WORKER), "x", entry)
    assert "meta" not in entry


@pytest.mark.asyncio
async def test_a_composer_steer_into_a_running_turn_reports_pending(monkeypatch):
    from aiohttp import web

    from kiro_crew.dashboard import chat_handlers
    from kiro_crew.dashboard.chat_api import chat_send
    from kiro_crew.dashboard.handlers import crew_workers

    async def fake_send(request):
        request[chat_send._CREW_PERSON_STEER] = (CONDUCTOR, WORKER, "x")
        return web.json_response({"ok": True, "steered": True})

    async def boom(*_a, **_k):
        raise AssertionError("an unread steer must not be recorded yet")

    monkeypatch.setattr(chat_handlers, "_api_chat_send", fake_send)
    monkeypatch.setattr(crew_workers, "person_steer", boom)
    resp = await chat_handlers.api_chat(_Req(SimpleNamespace()))  # type: ignore[arg-type]
    body = json.loads(resp.text)
    assert body["steered"] is True and body["ledger_pending"] is True
