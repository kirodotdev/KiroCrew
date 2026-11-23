"""The slot list's ``needs_full_chat``: what only the full chat can show waits on the person.

Each source is read from the store that owns it, without changing it: a change card the
person can still act on, a live guide offer, and a browser ``ask`` parked on the live card.
"""

from __future__ import annotations

import asyncio
import copy
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from change_cards_helpers import SLOT, Clock, _propose

from kiro_crew import change_card_catalog as catalog
from kiro_crew.browser_harness import live_hub as lh
from kiro_crew.dashboard import full_chat_waits
from kiro_crew.dashboard.change_cards import (
    INFLIGHT_STALE_SECONDS,
    STATUS_APPLIED,
    STATUS_APPLYING,
    STATUS_EXPIRED,
    STATUS_FAILED,
    STATUS_PARTIAL,
    STATUS_PENDING,
    CardStore,
)
from kiro_crew.dashboard.guide_runs import STATUS_COMPLETED, STATUS_OFFERED, GuideStore
from kiro_crew.dashboard.state import _ChatSlot

CREWMATE = {"id": "crewmate.create", "params": {"name": "Scout"}}


def _card(store: CardStore, status: str | None = None) -> dict:
    rec = store._cards[_propose(store)["id"]]
    if status is not None:
        rec["status"] = status
    return rec


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(clock: Clock) -> CardStore:
    return CardStore(None, clock=clock, key=lambda: b"k" * 32)


@pytest.mark.parametrize("status", [None, STATUS_FAILED, STATUS_APPLYING])
def test_a_card_the_person_can_still_act_on_is_open(store: CardStore, status) -> None:
    _card(store, status)

    assert store.holds_open_card(SLOT) is True
    assert store.holds_open_card("chat-other") is False


def test_a_finished_card_is_not_open(store: CardStore) -> None:
    _card(store, STATUS_APPLIED)

    assert store.holds_open_card(SLOT) is False


@pytest.mark.parametrize("status", [None, STATUS_FAILED])
def test_a_waiting_card_past_its_expiry_is_not_open_and_is_left_as_it_was(
    store: CardStore, clock: Clock, status
) -> None:
    rec = _card(store, status)
    before = rec["status"]
    clock.now = rec["expires_at"]

    assert store.holds_open_card(SLOT) is False
    assert rec["status"] == before


def _mid_apply(
    store: CardStore, *, status: str, repeat: bool, evidence: bool, inflight_age: float | None
):
    rec = _card(store, status)
    first = rec["plan"]["apply"][0]
    rec["plan"]["apply"] = [first, {**first, "method": "GET", "repeat": True} if repeat else first]
    rec["evidence"] = [{"ok": True}] if evidence else []
    if inflight_age is not None:
        rec["inflight"] = {
            "op": catalog.OP_APPLY,
            "step": 1,
            "started_at": rec["expires_at"] + 1 - inflight_age,
        }
    return rec


@pytest.mark.parametrize(
    ("repeat", "evidence", "inflight_age", "expected"),
    [
        pytest.param(True, False, None, False, id="abandoned_approval_poll_fails"),
        pytest.param(True, True, None, False, id="abandoned_poll_after_a_write_fails"),
        pytest.param(True, False, 60, True, id="poll_still_running_stays"),
        pytest.param(True, False, INFLIGHT_STALE_SECONDS + 1, False, id="stale_poll_is_abandoned"),
        pytest.param(False, True, None, True, id="plan_between_two_writes_stays"),
        pytest.param(False, False, None, False, id="plan_that_never_wrote_expires"),
        pytest.param(False, True, 60, True, id="write_still_running_stays"),
        pytest.param(False, True, INFLIGHT_STALE_SECONDS + 1, False, id="stale_write_needs_review"),
    ],
)
def test_an_applying_card_past_its_expiry_is_open_exactly_when_refresh_keeps_it_applying(
    store: CardStore, clock: Clock, repeat, evidence, inflight_age, expected
) -> None:
    rec = _mid_apply(
        store,
        status=STATUS_APPLYING,
        repeat=repeat,
        evidence=evidence,
        inflight_age=inflight_age,
    )
    clock.now = rec["expires_at"] + 1
    before = copy.deepcopy(rec)

    assert store.holds_open_card(SLOT) is expected
    assert rec == before
    store._refresh(rec)
    assert (rec["status"] == STATUS_APPLYING) is expected
    assert store.holds_open_card(SLOT) is expected


def test_a_dismissed_failed_card_is_not_open_because_pending_already_hides_it(
    store: CardStore,
) -> None:
    rec = _card(store, STATUS_FAILED)
    store.dismiss(rec)
    before = copy.deepcopy(rec)

    assert store.holds_open_card(SLOT) is False
    assert store.slots_with_open_cards() == frozenset()
    assert rec == before
    assert store.pending(SLOT) == []


@pytest.mark.parametrize("status", [STATUS_PENDING, STATUS_FAILED])
@pytest.mark.parametrize(
    ("repeat", "inflight_age", "expected"),
    [
        pytest.param(False, 60, True, id="write_still_running_stays"),
        pytest.param(False, INFLIGHT_STALE_SECONDS + 1, False, id="stale_write_needs_review"),
        pytest.param(True, INFLIGHT_STALE_SECONDS + 1, False, id="stale_poll_clears_and_expires"),
    ],
)
def test_a_waiting_card_past_its_expiry_is_open_exactly_when_refresh_keeps_it_waiting(
    store: CardStore, clock: Clock, status, repeat, inflight_age, expected
) -> None:
    """The store's own API never leaves ``inflight`` on a waiting card, so the read
    mirrors :meth:`_refresh` rather than re-deriving when a record stops waiting."""
    rec = _mid_apply(store, status=status, repeat=repeat, evidence=False, inflight_age=inflight_age)
    clock.now = rec["expires_at"] + 1
    before = copy.deepcopy(rec)

    assert store.holds_open_card(SLOT) is expected
    assert rec == before
    store._refresh(rec)
    assert (rec["status"] == status) is expected
    assert store.holds_open_card(SLOT) is expected


def test_a_waiting_card_whose_write_went_stale_is_not_open_before_its_expiry_either(
    store: CardStore, clock: Clock
) -> None:
    rec = _card(store, STATUS_PENDING)
    clock.now = rec["expires_at"] - 60
    rec["inflight"] = {
        "op": catalog.OP_APPLY,
        "step": 0,
        "started_at": clock.now - INFLIGHT_STALE_SECONDS - 1,
    }
    before = copy.deepcopy(rec)

    assert store.holds_open_card(SLOT) is False
    assert rec == before
    store._refresh(rec)
    assert rec["status"] == STATUS_PARTIAL


@pytest.mark.parametrize(
    ("outcome", "status_after", "open_before"),
    [
        pytest.param("keep", STATUS_PENDING, True, id="keep"),
        pytest.param("expire", STATUS_EXPIRED, False, id="expire"),
        pytest.param("review", STATUS_PARTIAL, False, id="review"),
    ],
)
def test_the_open_check_and_refresh_follow_the_same_verdict(
    store: CardStore, monkeypatch: pytest.MonkeyPatch, outcome, status_after, open_before
) -> None:
    """Change the one rule and both readers change with it: there is no second copy.

    The card is fresh, so only the patched verdict can expire it or send it to review.
    """
    rec = _card(store)
    verdict = (False, outcome)
    monkeypatch.setattr(
        CardStore, "_refresh_verdict", classmethod(lambda cls, r, now: verdict), raising=False
    )

    assert store.holds_open_card(SLOT) is open_before
    store._refresh(rec)
    assert rec["status"] == status_after


def test_a_cold_store_answers_false_without_loading(tmp_path) -> None:
    cold = CardStore(tmp_path / "cards.json", clock=Clock(), key=lambda: b"k" * 32)

    assert cold.holds_open_card(SLOT) is False
    assert cold._loaded is False


def _guide(store: GuideStore) -> str:
    return store.start(slot_key=SLOT, session_key=f"dashboard:{SLOT}", actions=[CREWMATE])[
        "guide_id"
    ]


def test_a_live_guide_offer_counts_until_it_expires_and_the_read_changes_nothing() -> None:
    now = [1000.0]
    store = GuideStore(clock=lambda: now[0])
    guide = store._guides[_guide(store)]

    assert store.holds_live_guide(SLOT) is True
    assert store.holds_live_guide("chat-other") is False

    now[0] = guide.expires_at
    assert store.holds_live_guide(SLOT) is False
    assert guide.status == STATUS_OFFERED


def test_a_finished_guide_does_not_count() -> None:
    store = GuideStore(clock=lambda: 1000.0)
    store._guides[_guide(store)].status = STATUS_COMPLETED

    assert store.holds_live_guide(SLOT) is False


def _ask(*, outcome: str = "", expires_in_ms: int = 60_000) -> SimpleNamespace:
    return SimpleNamespace(
        outcome=outcome,
        expires_at=int(time.time() * 1000) + expires_in_ms,
        settle=lambda _outcome: True,
    )


@pytest.fixture
def hub(monkeypatch: pytest.MonkeyPatch) -> lh.LiveHub:
    h = lh.LiveHub(clock=lambda: 100.0)
    monkeypatch.setattr(lh, "get_live_hub", lambda: h)
    return h


def _state(store: CardStore | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        _change_card_store=store or CardStore(None, clock=Clock(), key=lambda: b"k" * 32),
        _guide_store=GuideStore(clock=lambda: 1000.0),
        serving_loop=None,
        push_slots_update=MagicMock(),
    )


@pytest.mark.parametrize(
    ("make_ask", "expected"),
    [
        (lambda: None, False),
        (_ask, True),
        (lambda: _ask(outcome="filled"), False),
        (lambda: _ask(expires_in_ms=-1), False),
    ],
    ids=["no-ask", "parked", "answered", "past-deadline"],
)
def test_a_browser_ask_counts_only_while_parked(hub: lh.LiveHub, make_ask, expected) -> None:
    hub._session(SLOT).ask = make_ask()

    assert full_chat_waits.needs_full_chat(_state(), SLOT) is expected


def test_each_source_alone_sets_the_flag(hub: lh.LiveHub) -> None:
    state = _state()
    assert full_chat_waits.needs_full_chat(state, SLOT) is False

    _card(state._change_card_store)
    assert full_chat_waits.needs_full_chat(state, SLOT) is True

    state = _state()
    _guide(state._guide_store)
    assert full_chat_waits.needs_full_chat(state, SLOT) is True


def test_the_projection_carries_the_slot_reader_answer() -> None:
    slot = _ChatSlot(SLOT)
    assert slot.to_dict()["needs_full_chat"] is False

    slot._full_chat_waits = lambda key: key == SLOT
    assert slot.to_dict()["needs_full_chat"] is True

    slot._full_chat_waits = lambda key: "yes"
    assert slot.to_dict()["needs_full_chat"] is False


def test_the_startup_warm_loads_the_store_and_pushes_only_when_a_card_is_open(
    tmp_path, store: CardStore
) -> None:
    from kiro_crew.dashboard.handlers import change_cards as card_routes

    cold = CardStore(tmp_path / "cards.json", clock=Clock(), key=lambda: b"k" * 32)
    empty = _state(cold)
    asyncio.run(card_routes.warm_after_start(empty))
    assert cold._loaded is True
    assert empty.push_slots_update.call_count == 0

    _card(store)
    holding = _state(store)
    asyncio.run(card_routes.warm_after_start(holding))
    assert store.slots_with_open_cards() == frozenset({SLOT})
    assert holding.push_slots_update.call_count == 1


def test_the_gateway_route_table_loads_the_card_store_at_start() -> None:
    from aiohttp import web

    from kiro_crew.dashboard import server

    app = web.Application()
    server._register_mcp_routes(app)

    assert full_chat_waits.startup_warm in list(app.on_startup)


def test_the_startup_hook_returns_at_once_and_loads_the_store_in_a_task(tmp_path) -> None:
    cold = CardStore(tmp_path / "cards.json", clock=Clock(), key=lambda: b"k" * 32)

    async def scenario() -> tuple[bool, bool]:
        await full_chat_waits.startup_warm({"state": _state(cold)})
        loaded_when_hook_returned = cold._loaded
        await asyncio.gather(*list(full_chat_waits._STARTUP))
        return loaded_when_hook_returned, cold._loaded

    assert asyncio.run(scenario()) == (False, True)


def test_the_hub_tells_its_listener_when_an_ask_moves_or_its_turn_folds() -> None:
    async def scenario() -> list[str]:
        heard: list[str] = []
        h = lh.LiveHub(clock=lambda: 100.0)
        h.watch_asks(heard.append)
        h._session(SLOT).ask = _ask()
        await h.ask_changed(SLOT)
        h.turn_ended(SLOT)
        return heard

    assert asyncio.run(scenario()) == [SLOT, SLOT]


def test_every_card_and_guide_broadcast_pushes_the_slot_list(store: CardStore) -> None:
    from kiro_crew.dashboard.handlers import change_cards as card_routes
    from kiro_crew.dashboard.handlers import guide as guide_routes

    state = SimpleNamespace(push_slots_update=MagicMock())
    guides = GuideStore(clock=lambda: 1000.0)
    card = store.public(_card(store))
    guide = guides._guides[_guide(guides)].to_public()

    asyncio.run(card_routes._broadcast(state, card))
    asyncio.run(guide_routes._broadcast(state, guide))

    assert state.push_slots_update.call_count == 2


def test_the_gateway_hub_pushes_the_slot_list_when_an_ask_moves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.dashboard.handlers import browser_live

    h = lh.LiveHub(clock=lambda: 100.0)
    monkeypatch.setattr(browser_live, "get_live_hub", lambda: h)
    state = SimpleNamespace(push_slots_update=MagicMock())

    browser_live.bind_live_hub(state)
    asyncio.run(h.ask_changed(SLOT))

    assert state.push_slots_update.call_count == 1
