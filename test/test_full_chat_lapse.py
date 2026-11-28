"""The slot list is sent again when a ``needs_full_chat`` wait ends with nobody acting.

A change card past its expiry, a guide offer that expires and a browser ``ask`` past its
deadline move no store, so nothing else would tell an app host its flag is now false.
"""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest
from change_cards_helpers import SLOT, Clock, _propose

from kiro_crew import change_card_catalog as catalog
from kiro_crew.browser_harness import live_hub as lh
from kiro_crew.dashboard import full_chat_waits
from kiro_crew.dashboard.change_cards import (
    INFLIGHT_STALE_SECONDS,
    STATUS_APPLIED,
    STATUS_APPLYING,
    CardStore,
)
from kiro_crew.dashboard.guide_runs import STATUS_COMPLETED, GuideStore
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

CREWMATE = {"id": "crewmate.create", "params": {"name": "Scout"}}


class _State:
    """Hashable like the gateway's state, so it can own a lapse timer."""

    def __init__(self, loop: Any, cards: CardStore, guides: GuideStore) -> None:
        self._change_card_store = cards
        self._guide_store = guides
        self.serving_loop = loop
        self.flag_at_push: list[bool] = []
        self._pushed = asyncio.Condition() if loop is not None else None

    def push_slots_update(self) -> None:
        self.flag_at_push.append(full_chat_waits.needs_full_chat(self, SLOT))
        if self._pushed is not None:
            self.serving_loop.call_soon_threadsafe(asyncio.ensure_future, self._notify())

    async def _notify(self) -> None:
        async with self._pushed:
            self._pushed.notify_all()

    async def pushes(self, count: int) -> None:
        async def reached() -> None:
            async with self._pushed:
                await self._pushed.wait_for(lambda: len(self.flag_at_push) >= count)

        await asyncio.wait_for(reached(), FALLBACK_TIMEOUT_SECS)


@pytest.fixture
def hub(monkeypatch: pytest.MonkeyPatch) -> lh.LiveHub:
    h = lh.LiveHub(clock=lambda: 100.0)
    monkeypatch.setattr(lh, "get_live_hub", lambda: h)
    return h


def _guide(store: GuideStore) -> Any:
    gid = store.start(slot_key=SLOT, session_key=f"dashboard:{SLOT}", actions=[CREWMATE])[
        "guide_id"
    ]
    return store._guides[gid]


def _ask(expires_at_ms: int, outcome: str = "") -> SimpleNamespace:
    return SimpleNamespace(outcome=outcome, expires_at=expires_at_ms, settle=lambda _o: True)


class _LoopClock:
    """Frozen while a deadline is set, so a slow runner cannot end the wait early; then
    it ticks with the event loop's own clock, so a fired timer reads exactly as late."""

    def __init__(self) -> None:
        self.base = 1_000_000.0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._t0: float | None = None

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def __call__(self) -> float:
        if self._loop is None:
            return self.base
        if self._t0 is None:
            self._t0 = self._loop.time()
        return self.base + self._loop.time() - self._t0


@pytest.fixture
def loop_clock(monkeypatch: pytest.MonkeyPatch) -> _LoopClock:
    clock = _LoopClock()
    monkeypatch.setattr(full_chat_waits, "time", SimpleNamespace(time=clock))
    return clock


def _real_stores(clock: Any = time.time) -> tuple[CardStore, GuideStore]:
    return CardStore(None, clock=clock, key=lambda: b"k" * 32), GuideStore(clock=clock)


def _drop_timer(state: Any) -> asyncio.TimerHandle | None:
    held = full_chat_waits._LAPSE_TIMERS.pop(state, None)
    if held is None:
        return None
    held[1].cancel()
    return held[1]


def test_a_waiting_card_closes_at_its_expiry() -> None:
    clock = Clock()
    store = CardStore(None, clock=clock, key=lambda: b"k" * 32)
    rec = store._cards[_propose(store)["id"]]

    assert store.next_close_in() == pytest.approx(rec["expires_at"] - clock.now)
    clock.now = rec["expires_at"]
    assert store.next_close_in() is None


def test_a_running_step_may_close_its_card_when_it_goes_stale_before_the_expiry() -> None:
    clock = Clock()
    store = CardStore(None, clock=clock, key=lambda: b"k" * 32)
    rec = store._cards[_propose(store)["id"]]
    rec["inflight"] = {"op": catalog.OP_APPLY, "step": 0, "started_at": clock.now}

    assert store.next_close_in() == pytest.approx(
        min(INFLIGHT_STALE_SECONDS, rec["expires_at"] - clock.now)
    )


def test_an_applying_card_kept_past_its_expiry_has_nothing_to_wait_for() -> None:
    clock = Clock()
    store = CardStore(None, clock=clock, key=lambda: b"k" * 32)
    rec = store._cards[_propose(store)["id"]]
    rec["status"] = STATUS_APPLYING
    rec["evidence"] = [{"ok": True}]
    clock.now = rec["expires_at"] + 1

    assert store.holds_open_card(SLOT) is True
    assert store.next_close_in() is None


def test_a_finished_card_and_a_cold_store_have_nothing_to_wait_for(tmp_path) -> None:
    store = CardStore(None, clock=Clock(), key=lambda: b"k" * 32)
    store._cards[_propose(store)["id"]]["status"] = STATUS_APPLIED
    cold = CardStore(tmp_path / "cards.json", clock=Clock(), key=lambda: b"k" * 32)

    assert store.next_close_in() is None
    assert cold.next_close_in() is None
    assert cold._loaded is False


def test_a_live_guide_offer_lapses_at_its_expiry_and_a_finished_one_never() -> None:
    now = [1000.0]
    store = GuideStore(clock=lambda: now[0])
    guide = _guide(store)

    assert store.next_lapse_in() == pytest.approx(guide.expires_at - now[0])
    guide.status = STATUS_COMPLETED
    assert store.next_lapse_in() is None


def test_the_hub_names_the_first_future_deadline_of_an_unanswered_ask(hub: lh.LiveHub) -> None:
    hub._session("chat-a").ask = _ask(5_000)
    hub._session("chat-b").ask = _ask(3_000, outcome="filled")
    hub._session("chat-c").ask = _ask(4_000)
    hub._session("chat-d").ask = _ask(1_000)

    assert hub.first_ask_deadline_ms(2_000) == 4_000
    assert hub.first_ask_deadline_ms(5_000) is None


def test_a_lapsed_ask_still_bound_does_not_hide_another_sessions_deadline(
    hub: lh.LiveHub, loop_clock: _LoopClock
) -> None:
    hub._session("chat-other").ask = _ask(int((loop_clock() - 60) * 1000))
    hub._session(SLOT).ask = _ask(int((loop_clock() + 30) * 1000))

    assert full_chat_waits.next_lapse_in(_State(None, *_real_stores(loop_clock))) == (
        pytest.approx(30, abs=1)
    )


@pytest.mark.parametrize("source", ["card", "guide", "ask"])
def test_the_slot_list_is_sent_again_once_the_wait_ends_by_itself(
    hub: lh.LiveHub, loop_clock: _LoopClock, source: str
) -> None:
    async def scenario() -> _State:
        cards, guides = _real_stores(loop_clock)
        state = _State(asyncio.get_running_loop(), cards, guides)
        ends_at = loop_clock() + 0.05
        if source == "card":
            cards._cards[_propose(cards)["id"]]["expires_at"] = ends_at
        elif source == "guide":
            _guide(guides).expires_at = ends_at
        else:
            hub._session(SLOT).ask = _ask(int(ends_at * 1000))
        loop_clock.start(asyncio.get_running_loop())
        full_chat_waits.push_slot_list(state)
        await state.pushes(2)
        return state

    state = asyncio.run(scenario())

    assert state.flag_at_push == [True, False]
    assert state not in full_chat_waits._LAPSE_TIMERS


def test_each_fire_times_the_next_wait_and_the_last_one_sets_no_timer(hub: lh.LiveHub) -> None:
    clock = Clock()
    cards = CardStore(None, clock=clock, key=lambda: b"k" * 32)
    guides = GuideStore(clock=clock)
    start = clock.now
    _guide(guides).expires_at = start + 10
    cards._cards[_propose(cards)["id"]]["expires_at"] = start + 20

    loop_now = 5_000.0

    async def scenario() -> tuple[list[float | None], list[bool]]:
        loop = asyncio.get_running_loop()
        loop.time = lambda: loop_now
        state = _State(loop, cards, guides)
        seen: list[float | None] = []
        full_chat_waits.push_slot_list(state)
        for now in (start + 10, start + 20):
            timer = _drop_timer(state)
            seen.append(None if timer is None else timer.when())
            clock.now = now
            full_chat_waits._lapsed(state)
        seen.append(None if _drop_timer(state) is None else 0.0)
        return seen, state.flag_at_push

    seen, flags = asyncio.run(scenario())

    slack = full_chat_waits.LAPSE_SLACK_S
    assert seen[0] == pytest.approx(loop_now + 10 + slack)
    assert seen[1] == pytest.approx(loop_now + 10 + slack)
    assert seen[2] is None
    assert flags == [True, True, False]


def test_a_push_from_another_thread_sets_the_timer_on_the_serving_loop(
    hub: lh.LiveHub, loop_clock: _LoopClock
) -> None:
    async def scenario() -> _State:
        cards, guides = _real_stores(loop_clock)
        state = _State(asyncio.get_running_loop(), cards, guides)
        cards._cards[_propose(cards)["id"]]["expires_at"] = loop_clock() + 0.05
        loop_clock.start(asyncio.get_running_loop())
        worker = threading.Thread(target=full_chat_waits.push_slot_list, args=(state,))
        worker.start()
        await asyncio.to_thread(worker.join)
        await state.pushes(2)
        return state

    assert asyncio.run(scenario()).flag_at_push == [True, False]


def test_a_later_push_moves_the_timer_earlier_but_never_later(hub: lh.LiveHub) -> None:
    async def scenario() -> list[bool]:
        cards, guides = _real_stores()
        state = _State(asyncio.get_running_loop(), cards, guides)
        cards._cards[_propose(cards)["id"]]["expires_at"] = time.time() + 60
        full_chat_waits.push_slot_list(state)
        far = full_chat_waits._LAPSE_TIMERS[state][1]
        _guide(guides).expires_at = time.time() + 30
        full_chat_waits.push_slot_list(state)
        near = full_chat_waits._LAPSE_TIMERS[state][1]
        full_chat_waits._place(state, state.serving_loop, far.when())
        kept = _drop_timer(state)
        return [far.cancelled(), near is not far, kept is near]

    assert asyncio.run(scenario()) == [True, True, True]


def test_a_timer_left_on_a_closed_loop_does_not_stop_the_next_loop_getting_one(
    hub: lh.LiveHub, loop_clock: _LoopClock
) -> None:
    cards, guides = _real_stores(loop_clock)
    rec = cards._cards[_propose(cards)["id"]]
    rec["expires_at"] = loop_clock() + 0.05
    state = _State(None, cards, guides)

    async def first() -> None:
        state.serving_loop = asyncio.get_running_loop()
        full_chat_waits.push_slot_list(state)

    async def second() -> list[bool]:
        state.serving_loop = asyncio.get_running_loop()
        state._pushed = asyncio.Condition()
        loop_clock.start(state.serving_loop)
        full_chat_waits.push_slot_list(state)
        await state.pushes(3)
        return state.flag_at_push

    asyncio.run(first())
    assert asyncio.run(second()) == [True, True, False]


def test_a_wait_that_ends_during_the_push_still_gets_its_timer(hub: lh.LiveHub) -> None:
    """The frame the push takes still shows the wait, so a lapse read after it is too late."""
    clock = Clock()
    cards = CardStore(None, clock=clock, key=lambda: b"k" * 32)
    rec = cards._cards[_propose(cards)["id"]]

    class _SlowPush(_State):
        def push_slots_update(self) -> None:
            super().push_slots_update()
            clock.now = rec["expires_at"]

    async def scenario() -> tuple[list[bool], bool]:
        state = _SlowPush(asyncio.get_running_loop(), cards, GuideStore(clock=clock))
        full_chat_waits.push_slot_list(state)
        return state.flag_at_push, _drop_timer(state) is not None

    assert asyncio.run(scenario()) == ([True], True)


def test_no_timer_without_a_serving_loop_or_a_wait(hub: lh.LiveHub) -> None:
    loopless = _State(None, *_real_stores())
    full_chat_waits.push_slot_list(loopless)
    assert loopless not in full_chat_waits._LAPSE_TIMERS

    async def scenario() -> bool:
        state = _State(asyncio.get_running_loop(), *_real_stores())
        full_chat_waits.push_slot_list(state)
        return state in full_chat_waits._LAPSE_TIMERS

    assert asyncio.run(scenario()) is False
