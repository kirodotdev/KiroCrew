"""A chat that stopped on a usage limit can be continued once, just after it resets.

The limit row's ``reset_at`` (an ISO instant, read from the provider's own sentence)
is what the error card offers "Continue automatically after <time>" for. Pressing the
button arms one wait in the gateway; the wait ends with the same Continue the card's
Resume button runs. Pinned here:

* the row a chat ends on decides everything: only a usage-limit row naming a readable
  time counts, and a message typed after it moves the chat on;
* the wait ends a margin after the named time on a clock the test advances in whole
  seconds, reading the wall clock again at least once a slice so a suspended host
  does not delay it, resumes once, and does nothing when the chat moved on (a message, a turn,
  a close), is held by a conductor mode pause, is a relay archive, or the gateway is
  stopping, and it checks all of that again under the slot's lock;
* the wait holds the chat only weakly, and shutdown also stops a continue under way;
* the route answers 200 with ``ok`` false and a warning for everything it cannot
  honour (a time already past, a time that is not the row's, a chat with no limit row)
  and never 400; a time the browser cut to milliseconds is still the row's; a malformed body is dropped with a warning and the row decides; only
  a missing chat is 404;
* an armed chat's slot summary says so, which is how a reloaded card learns it.
"""

from __future__ import annotations

import asyncio
import gc
import weakref
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from dashboard_owner_helpers import as_owner

from kiro_crew.dashboard import limit_continue
from kiro_crew.testing import wait as wait_module
from kiro_crew.testing.clock import ManualClock
from kiro_crew.testing.wait import await_until

START = 1_800_000_000
#: A reset an hour ahead that carries the clock's microseconds, as a relative clause gives it.
_SUB_SECOND = datetime.fromtimestamp(START + 3600, tz=timezone.utc).replace(microsecond=123_456)


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _limit_row(reset_epoch: int | None, *, kind: str = "usage_limit") -> dict:
    meta: dict = {"kind": kind}
    if reset_epoch is not None:
        meta["reset_at"] = _iso(reset_epoch)
    return {"role": "error", "content": "limit", "meta": meta}


@pytest.fixture(autouse=True)
def _clock_and_no_waits_left_over(_floor_monkeypatch):
    clock = ManualClock(START)
    clock.install(_floor_monkeypatch, limit_continue)
    yield clock
    for key in list(limit_continue._ARMED):
        limit_continue.cancel(key)
    limit_continue._STARTED.clear()


@pytest.fixture
def clock(_clock_and_no_waits_left_over) -> ManualClock:
    return _clock_and_no_waits_left_over


class TestLimitResetOf:
    def test_the_newest_error_row_naming_a_time(self):
        rows = [
            {"role": "user", "content": "go"},
            _limit_row(START + 3600),
            {"role": "inject", "content": ""},
        ]
        assert limit_continue.limit_reset_of(rows) == START + 3600

    def test_a_message_typed_after_the_limit_moves_the_chat_on(self):
        rows = [_limit_row(START + 3600), {"role": "user", "content": "try again"}]
        assert limit_continue.limit_reset_of(rows) is None

    @pytest.mark.parametrize(
        "row",
        [
            _limit_row(None),
            _limit_row(START + 3600, kind="auth_required"),
            {"role": "error", "content": "x", "meta": {"kind": "usage_limit", "reset_at": 5}},
            {"role": "error", "content": "x", "meta": {"kind": "usage_limit", "reset_at": "soon"}},
            # An instant with no zone is not one the gateway wrote.
            {"role": "error", "meta": {"kind": "usage_limit", "reset_at": "2026-11-15T09:00:00"}},
            {"role": "error", "content": "x", "meta": "usage_limit"},
        ],
    )
    def test_a_row_that_names_no_readable_time_has_none(self, row):
        assert limit_continue.limit_reset_of([row]) is None

    def test_an_empty_chat_has_none(self):
        assert limit_continue.limit_reset_of([]) is None


class _Chat:
    """The parts of a chat slot the wait reads; weakly referenceable, as a real slot is."""

    def __init__(self, key: str, rows: list[dict]) -> None:
        self.key = key
        self.messages = rows
        self._turns_started = 0
        self.executor = ""


def _slot(key: str, rows: list[dict]) -> _Chat:
    return _Chat(key, rows)


def _state(*slots: _Chat) -> SimpleNamespace:
    return SimpleNamespace(_slots={s.key: s for s in slots}, push_slots_update=lambda: None)


@pytest.fixture
def resume(_floor_monkeypatch) -> AsyncMock:
    go = AsyncMock(return_value="resumed")
    _floor_monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.continue_slot_turn", go)
    return go


async def _drain() -> None:
    """Wait for every continue under way; one that never ends fails the test by name."""
    while limit_continue._FIRING:
        await await_until(
            asyncio.gather(*list(limit_continue._FIRING)),
            what="a continue under way to finish",
        )


class TestTheDrain:
    @pytest.mark.asyncio
    async def test_a_continue_that_never_ends_fails_the_test_instead_of_hanging_it(
        self, _floor_monkeypatch
    ):
        # The wait's bound is half the running test's timeout; make that bound tiny.
        _floor_monkeypatch.setattr(
            wait_module, "runner_timeout_secs", 0.02  # flake-ok: reaches the expiry path
        )
        stuck = asyncio.ensure_future(asyncio.get_running_loop().create_future())
        limit_continue._FIRING.add(stuck)
        try:
            with pytest.raises(AssertionError, match="a continue under way to finish"):
                await _drain()
        finally:
            limit_continue._FIRING.discard(stuck)
            stuck.cancel()


class TestTheWait:
    @pytest.mark.asyncio
    async def test_it_ends_a_margin_after_the_named_time(self):
        slot = _slot("s1", [_limit_row(START + 3600)])
        armed = limit_continue.arm(_state(slot), slot, START + 3600)
        assert armed.resume_at == START + 3600 + limit_continue.RESUME_MARGIN_SECS
        assert limit_continue._ARMED["s1"] is armed

    @pytest.mark.asyncio
    async def test_a_timer_that_ends_early_waits_out_what_the_clock_owes(self, clock, resume):
        slot = _slot("s1", [_limit_row(START + 3600)])
        state = _state(slot)
        armed = limit_continue.arm(state, slot, START + 3600)
        first_handle = armed.handle

        clock.advance(3659)  # one second short of the margin
        limit_continue._wake(state, armed)
        await _drain()
        resume.assert_not_awaited()
        assert armed.handle is not first_handle
        assert limit_continue._ARMED["s1"] is armed

        clock.advance(1)
        limit_continue._wake(state, armed)
        await _drain()
        resume.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_host_suspended_through_the_reset_continues_within_one_slice(
        self, clock, resume, _floor_monkeypatch
    ):
        # The loop's timer does not count a suspended host, so no timer runs longer
        # than one slice before the wall clock is read again.
        loop = asyncio.get_running_loop()
        delays: list[float] = []
        real_call_later = loop.call_later

        def _spy(delay, *args):
            delays.append(delay)
            return real_call_later(delay, *args)

        _floor_monkeypatch.setattr(loop, "call_later", _spy)
        slot = _slot("s1", [_limit_row(START + 5 * 3600)])
        state = _state(slot)
        armed = limit_continue.arm(state, slot, START + 5 * 3600)
        clock.advance(3600)
        limit_continue._wake(state, armed)  # an hour in: not due, so one more slice
        await _drain()
        resume.assert_not_awaited()
        assert len(delays) == 2
        assert max(delays) <= limit_continue.WALL_RECHECK_SECS
        # The host sleeps through the reset; the slice that ends on waking finds it due.
        clock.advance(5 * 3600)
        limit_continue._wake(state, armed)
        await _drain()
        resume.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_it_continues_a_chat_still_at_the_limit_once(self, clock, resume):
        slot = _slot("s1", [_limit_row(START + 3600)])
        state = _state(slot)
        armed = limit_continue.arm(state, slot, START + 3600)
        clock.advance(3660)
        limit_continue._wake(state, armed)
        limit_continue._wake(state, armed)  # a second timer for the same wait
        await _drain()
        resume.assert_awaited_once()
        assert resume.await_args.args[:2] == (state, slot)
        assert resume.await_args.kwargs["via"] == "usage_limit_reset"
        assert "s1" not in limit_continue._ARMED

    @pytest.mark.asyncio
    async def test_a_message_typed_meanwhile_ends_it_without_a_continue(self, clock, resume):
        slot = _slot("s1", [_limit_row(START + 3600)])
        state = _state(slot)
        armed = limit_continue.arm(state, slot, START + 3600)
        slot.messages.append({"role": "user", "content": "I will do it myself"})
        clock.advance(3660)
        limit_continue._wake(state, armed)
        await _drain()
        resume.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_newer_limit_row_is_not_the_one_armed(self, clock, resume):
        slot = _slot("s1", [_limit_row(START + 3600)])
        state = _state(slot)
        armed = limit_continue.arm(state, slot, START + 3600)
        slot.messages.append(_limit_row(START + 7200))
        clock.advance(3660)
        limit_continue._wake(state, armed)
        await _drain()
        resume.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_closed_or_replaced_chat_is_left_alone(self, clock, resume):
        slot = _slot("s1", [_limit_row(START + 3600)])
        state = _state(slot)
        armed = limit_continue.arm(state, slot, START + 3600)
        clock.advance(3660)
        state._slots["s1"] = _slot("s1", [_limit_row(START + 3600)])
        limit_continue._wake(state, armed)
        await _drain()
        armed2 = limit_continue.arm(state, slot, START + 3600)
        state._slots.clear()
        limit_continue._wake(state, armed2)
        await _drain()
        resume.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("code", ["slot_running", "slot_not_found"])
    async def test_a_refused_continue_does_not_raise(self, clock, resume, code):
        from kiro_crew.dashboard.chat_handlers import SlotContinueRefusal

        resume.side_effect = SlotContinueRefusal("busy", code)
        slot = _slot("s1", [_limit_row(START + 3600)])
        state = _state(slot)
        armed = limit_continue.arm(state, slot, START + 3600)
        clock.advance(3660)
        limit_continue._wake(state, armed)
        await _drain()
        resume.assert_awaited_once()
        assert not limit_continue._FIRING

    @pytest.mark.asyncio
    async def test_a_turn_since_arming_ends_it_even_when_it_hit_the_same_limit(self, clock, resume):
        slot = _slot("s1", [_limit_row(START + 3600)])
        state = _state(slot)
        armed = limit_continue.arm(state, slot, START + 3600)
        # The person typed, the turn ran, and it stopped on the same limit again.
        slot._turns_started += 1
        slot.messages += [{"role": "user", "content": "go on"}, _limit_row(START + 3600)]
        clock.advance(3660)
        limit_continue._wake(state, armed)
        await _drain()
        resume.assert_not_awaited()
        assert limit_continue.armed_resume_iso(slot) is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("held_by", ["pause", "relay_archive", "shutdown"])
    async def test_a_paused_archived_or_stopping_chat_is_left_alone(
        self, clock, resume, _floor_monkeypatch, held_by
    ):
        slot = _slot("s1", [_limit_row(START + 3600)])
        state = _state(slot)
        armed = limit_continue.arm(state, slot, START + 3600)
        if held_by == "pause":
            _floor_monkeypatch.setattr("kiro_crew.crew_pause.holds_slot", lambda s: s is slot)
        elif held_by == "relay_archive":
            slot.executor = "remote"
        else:
            _floor_monkeypatch.setattr(
                "kiro_crew.dashboard.goal_reminder.gateway_stopping", lambda: True
            )
        clock.advance(3660)
        limit_continue._wake(state, armed)
        await _drain()
        resume.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_it_checks_again_under_the_lock_just_before_the_continue_is_queued(
        self, clock, resume
    ):
        from kiro_crew.dashboard.chat_handlers import SlotContinueRefusal

        slot = _slot("s1", [_limit_row(START + 3600)])
        state = _state(slot)
        armed = limit_continue.arm(state, slot, START + 3600)
        clock.advance(3660)
        limit_continue._wake(state, armed)
        await _drain()
        kwargs = resume.await_args.kwargs
        kwargs["before_dispatch"]()  # nothing changed: it lets the continue through
        assert kwargs["replaced"]() is False
        # A message that landed while the continue waited for the slot's lock.
        slot.messages.append({"role": "user", "content": "typed while it waited"})
        with pytest.raises(SlotContinueRefusal):
            kwargs["before_dispatch"]()
        state._slots["s1"] = _slot("s1", [_limit_row(START + 3600)])
        assert kwargs["replaced"]() is True

    @pytest.mark.asyncio
    async def test_a_closed_chat_is_not_kept_alive_and_its_wait_ends_at_the_next_wake(
        self, clock, resume
    ):
        slot = _slot("s1", [_limit_row(START + 3600)])
        state = _state(slot)
        armed = limit_continue.arm(state, slot, START + 3600)
        gone = weakref.ref(slot)
        del state._slots["s1"]
        del slot
        gc.collect()
        assert gone() is None
        clock.advance(60)  # long before the reset
        limit_continue._wake(state, armed)
        assert "s1" not in limit_continue._ARMED
        await _drain()
        resume.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_shutdown_also_stops_a_continue_already_under_way(self, clock, resume):
        started, release = asyncio.Event(), asyncio.Event()

        async def _slow(*_args, **_kwargs):
            started.set()
            await release.wait()

        resume.side_effect = _slow
        slot = _slot("s1", [_limit_row(START + 3600)])
        state = _state(slot)
        armed = limit_continue.arm(state, slot, START + 3600)
        clock.advance(3660)
        limit_continue._wake(state, armed)
        (task,) = limit_continue._FIRING
        await await_until(asyncio.ensure_future(started.wait()))
        await limit_continue.cancel_all(None)
        assert task.cancelled()
        assert not limit_continue._FIRING

    @staticmethod
    async def _fire_held_at_the_probe(clock, resume):
        """Start the continue and hold it where it awaits its probes, before the insert."""
        probing, release = asyncio.Event(), asyncio.Event()
        queued: list[str] = []

        async def _probing(*_args, before_dispatch, **_kwargs):
            probing.set()
            await release.wait()
            before_dispatch()  # under the slot's lock, just before the insert
            queued.append("turn")
            return "resumed"

        resume.side_effect = _probing
        slot = _slot("s1", [_limit_row(START + 3600)])
        state = _state(slot)
        armed = limit_continue.arm(state, slot, START + 3600)
        clock.advance(3660)
        limit_continue._wake(state, armed)
        await await_until(asyncio.ensure_future(probing.wait()))
        return slot, release, queued

    @pytest.mark.asyncio
    async def test_cancel_while_the_continue_awaits_its_probes_stops_it(self, clock, resume):
        slot, release, queued = await self._fire_held_at_the_probe(clock, resume)
        # The card still shows Cancel while the continue is under way.
        assert limit_continue.armed_resume_iso(slot) == _iso(START + 3660)
        state = _state(slot)
        assert await TestTheRoutes._call(state, "DELETE", "s1") == (
            200,
            {"ok": True, "cancelled": True},
        )
        release.set()
        await _drain()
        assert queued == []
        assert "s1" not in limit_continue._ARMED

    @pytest.mark.asyncio
    async def test_a_queued_continue_answers_started_until_the_continue_returns(
        self, clock, resume
    ):
        inserted, release = asyncio.Event(), asyncio.Event()

        async def _queued_then_starting(*_args, before_dispatch, **_kwargs):
            before_dispatch()  # the insert is done; the turn is starting
            inserted.set()
            await release.wait()
            return "resumed"

        resume.side_effect = _queued_then_starting
        slot = _slot("s1", [_limit_row(START + 3600)])
        state = _state(slot)
        armed = limit_continue.arm(state, slot, START + 3600)
        clock.advance(3660)
        limit_continue._wake(state, armed)
        await await_until(asyncio.ensure_future(inserted.wait()))
        assert limit_continue.started("s1") is True
        assert limit_continue.cancel("s1") is False
        assert limit_continue.armed_resume_iso(slot) is None
        assert await TestTheRoutes._call(state, "DELETE", "s1") == (
            200,
            {"ok": True, "cancelled": False, "started": True},
        )
        release.set()
        await _drain()
        assert "s1" not in limit_continue._ARMED
        assert limit_continue.started("s1") is False

    @staticmethod
    async def _continue_that_queues(clock, resume):
        """Run the wait to its end, the continue queueing its turn and returning."""

        async def _queues(*_args, before_dispatch, **_kwargs):
            before_dispatch()
            return "resumed"

        resume.side_effect = _queues
        slot = _slot("s1", [_limit_row(START + 3600)])
        state = _state(slot)
        armed = limit_continue.arm(state, slot, START + 3600)
        clock.advance(3660)
        limit_continue._wake(state, armed)
        await _drain()
        assert "s1" not in limit_continue._ARMED
        return slot, state

    @pytest.mark.asyncio
    async def test_a_late_cancel_after_the_continue_returned_still_answers_started(
        self, clock, resume
    ):
        slot, state = await self._continue_that_queues(clock, resume)
        slot._turns_started = 1  # the continued turn itself
        for _ in range(2):  # a repeated Cancel gets the same answer
            assert await TestTheRoutes._call(state, "DELETE", "s1") == (
                200,
                {"ok": True, "cancelled": False, "started": True},
            )
        assert limit_continue.armed_resume_iso(slot) is None

    @pytest.mark.asyncio
    async def test_the_started_answer_ends_with_the_next_turn_or_after_its_time_to_live(
        self, clock, resume
    ):
        slot, state = await self._continue_that_queues(clock, resume)
        slot._turns_started = 2  # a turn after the continued one
        assert await TestTheRoutes._call(state, "DELETE", "s1") == (
            200,
            {"ok": True, "cancelled": False},
        )
        slot, state = await self._continue_that_queues(clock, resume)
        clock.advance(limit_continue.STARTED_TTL_SECS)
        assert await TestTheRoutes._call(state, "DELETE", "s1") == (
            200,
            {"ok": True, "cancelled": False},
        )
        assert "s1" not in limit_continue._STARTED

    @pytest.mark.asyncio
    async def test_a_continue_that_queued_nothing_leaves_no_started_answer(self, clock, resume):
        slot = _slot("s1", [_limit_row(START + 3600)])
        state = _state(slot)
        armed = limit_continue.arm(state, slot, START + 3600)
        slot.messages.append({"role": "user", "content": "typed first"})
        clock.advance(3660)
        limit_continue._wake(state, armed)
        await _drain()
        assert await TestTheRoutes._call(state, "DELETE", "s1") == (
            200,
            {"ok": True, "cancelled": False},
        )

    @pytest.mark.asyncio
    async def test_a_new_wait_or_shutdown_drops_the_started_record_and_the_record_is_bounded(
        self, clock, resume
    ):
        slot, state = await self._continue_that_queues(clock, resume)
        assert limit_continue.started("s1", slot) is True
        limit_continue.arm(state, slot, START + 7200)
        assert limit_continue.started("s1", slot) is False
        slot, state = await self._continue_that_queues(clock, resume)
        await limit_continue.cancel_all(None)
        assert not limit_continue._STARTED
        chats = [_slot(f"k{n}", []) for n in range(limit_continue.STARTED_CAP + 20)]
        for chat in chats:
            limit_continue._remember_started(chat, turns=0)
        assert len(limit_continue._STARTED) == limit_continue.STARTED_CAP

    @pytest.mark.asyncio
    async def test_cancel_drops_the_wait_and_a_cancelled_timer_does_nothing(self, clock, resume):
        slot = _slot("s1", [_limit_row(START + 3600)])
        state = _state(slot)
        armed = limit_continue.arm(state, slot, START + 3600)
        assert limit_continue.cancel("s1") is True
        assert armed.handle.cancelled()
        assert limit_continue.cancel("s1") is False
        clock.advance(3660)
        limit_continue._wake(state, armed)
        await _drain()
        resume.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_arming_again_replaces_the_earlier_wait(self):
        slot = _slot("s1", [_limit_row(START + 3600)])
        state = _state(slot)
        first = limit_continue.arm(state, slot, START + 3600)
        second = limit_continue.arm(state, slot, START + 3600)
        assert first.handle.cancelled()
        assert limit_continue._ARMED["s1"] is second

    @pytest.mark.asyncio
    async def test_shutdown_drops_every_wait(self):
        a, b = _slot("a", [_limit_row(START + 60)]), _slot("b", [_limit_row(START + 60)])
        state = _state(a, b)
        handles = [limit_continue.arm(state, s, START + 60).handle for s in (a, b)]
        await limit_continue.cancel_all(None)
        assert not limit_continue._ARMED
        assert all(h.cancelled() for h in handles)

    @pytest.mark.asyncio
    async def test_the_summary_reports_an_armed_wait_until_the_chat_moves_on(self):
        slot = _slot("s1", [_limit_row(START + 3600)])
        assert limit_continue.armed_resume_iso(slot) is None
        limit_continue.arm(_state(slot), slot, START + 3600)
        assert limit_continue.armed_resume_iso(slot) == _iso(START + 3660)
        slot.messages.append({"role": "user", "content": "never mind"})
        assert limit_continue.armed_resume_iso(slot) is None
        assert "s1" not in limit_continue._ARMED


class TestTheRoutes:
    @staticmethod
    async def _call(
        state,
        method: str,
        key: str,
        body=None,
        *,
        raw: bytes | None = None,
        headers: dict | None = None,
    ):
        app = web.Application()
        app["state"] = state
        app.router.add_post(
            "/api/chat/slots/{slot}/limit-continue", limit_continue.api_chat_slot_limit_continue
        )
        app.router.add_delete(
            "/api/chat/slots/{slot}/limit-continue",
            limit_continue.api_chat_slot_limit_continue_cancel,
        )
        async with TestClient(TestServer(as_owner(app))) as client:
            path = f"/api/chat/slots/{key}/limit-continue"
            kwargs: dict = {"headers": dict(headers or {})}
            if raw is not None:
                kwargs = {
                    "data": raw,
                    "headers": {"Content-Type": "application/json", **kwargs["headers"]},
                }
            elif body is not None:
                kwargs["json"] = body
            response = await await_until(
                asyncio.ensure_future(client.request(method, path, **kwargs))
            )
            return response.status, await response.json()

    @pytest.mark.asyncio
    async def test_arms_the_time_the_chats_limit_row_names(self):
        slot = _slot("s1", [_limit_row(START + 3600)])
        status, body = await self._call(
            _state(slot), "POST", "s1", {"reset_at": _iso(START + 3600)}
        )
        assert (status, body) == (
            200,
            {"ok": True, "slot": "s1", "continue_at": _iso(START + 3660)},
        )
        assert limit_continue._ARMED["s1"].reset_at == START + 3600

    @pytest.mark.asyncio
    @pytest.mark.parametrize("raw", [b"", b"{}", b'{"reset_at": null}'])
    async def test_a_body_that_names_no_time_lets_the_row_decide(self, raw):
        slot = _slot("s1", [_limit_row(START + 3600)])
        status, body = await self._call(_state(slot), "POST", "s1", raw=raw)
        assert (status, body["ok"]) == (200, True)
        assert "warning" not in body
        assert limit_continue._ARMED["s1"].reset_at == START + 3600

    @pytest.mark.asyncio
    @pytest.mark.parametrize("raw", [b"not json", b"[1]", b"null", b'"s"', b"\xff"])
    async def test_a_malformed_body_is_dropped_with_a_warning_and_the_row_decides(self, raw):
        slot = _slot("s1", [_limit_row(START + 3600)])
        status, body = await self._call(_state(slot), "POST", "s1", raw=raw)
        assert (status, body["ok"]) == (200, True)
        assert "JSON object" in body["warning"]
        assert limit_continue._ARMED["s1"].reset_at == START + 3600

    @pytest.mark.asyncio
    @pytest.mark.parametrize("sent", [5, True, "soon", "2026-11-15T09:00:00", ["x"]])
    async def test_a_time_that_is_not_an_instant_is_dropped_with_a_warning(self, sent):
        slot = _slot("s1", [_limit_row(START + 3600)])
        status, body = await self._call(_state(slot), "POST", "s1", {"reset_at": sent})
        assert (status, body["ok"]) == (200, True)
        assert "reset_at" in body["warning"]
        assert limit_continue._ARMED["s1"].reset_at == START + 3600

    @pytest.mark.asyncio
    async def test_the_pressed_time_is_not_the_rows(self):
        slot = _slot("s1", [_limit_row(START + 3600)])
        status, body = await self._call(
            _state(slot), "POST", "s1", {"reset_at": _iso(START + 7200)}
        )
        assert status == 200 and body["ok"] is False and body["warning"]
        assert "s1" not in limit_continue._ARMED

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "sent, armed",
        [
            # The row's own string, as the card sends it.
            (_SUB_SECOND.isoformat(), True),
            # The same instant cut to milliseconds, as an older card sends it.
            (_SUB_SECOND.isoformat(timespec="milliseconds").replace("+00:00", "Z"), True),
            # Two milliseconds off is another time.
            (_SUB_SECOND.replace(microsecond=125_456).isoformat(), False),
        ],
    )
    async def test_a_reset_with_microseconds_arms_from_its_own_string_or_milliseconds(
        self, sent, armed
    ):
        # A relative clause ("resets in 2 hours") gives the row the clock's microseconds.
        row = {"role": "error", "content": "limit", "meta": {"kind": "usage_limit"}}
        row["meta"]["reset_at"] = _SUB_SECOND.isoformat()
        slot = _slot("s1", [row])
        status, body = await self._call(_state(slot), "POST", "s1", {"reset_at": sent})
        assert (status, body["ok"]) == (200, armed)
        assert ("s1" in limit_continue._ARMED) is armed

    @pytest.mark.asyncio
    @pytest.mark.parametrize("offset", [0, -1, -3600])
    async def test_a_time_already_past_arms_nothing_and_is_not_an_error(self, offset):
        slot = _slot("s1", [_limit_row(START + offset)])
        status, body = await self._call(
            _state(slot), "POST", "s1", {"reset_at": _iso(START + offset)}
        )
        assert status == 200 and body["ok"] is False and "passed" in body["warning"]
        assert "s1" not in limit_continue._ARMED

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "rows",
        [
            [],
            [_limit_row(None)],
            [_limit_row(START + 3600), {"role": "user", "content": "typed since"}],
        ],
    )
    async def test_a_chat_that_no_longer_ends_on_a_limit_arms_nothing(self, rows):
        status, body = await self._call(
            _state(_slot("s1", rows)), "POST", "s1", {"reset_at": _iso(START + 3600)}
        )
        assert status == 200 and body["ok"] is False and body["warning"]
        assert "s1" not in limit_continue._ARMED

    @pytest.mark.asyncio
    async def test_a_second_press_replaces_the_first_wait(self):
        slot = _slot("s1", [_limit_row(START + 3600)])
        state = _state(slot)
        await self._call(state, "POST", "s1")
        first = limit_continue._ARMED["s1"]
        status, body = await self._call(state, "POST", "s1")
        assert (status, body["ok"]) == (200, True)
        assert limit_continue._ARMED["s1"] is not first
        assert first.handle.cancelled()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", ["POST", "DELETE"])
    async def test_a_missing_chat_is_a_404(self, method):
        status, body = await self._call(_state(), method, "nope")
        assert (status, body["code"]) == (404, "slot_not_found")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", ["POST", "DELETE"])
    async def test_an_app_token_gets_the_same_refusal_for_a_missing_and_a_live_chat(self, method):
        slot = _slot("s1", [_limit_row(START + 3600)])
        state = _state(slot)
        app = {"X-Test-App": "some-app"}
        live = await self._call(state, method, "s1", headers=app)
        missing = await self._call(state, method, "nope", headers=app)
        assert live == missing
        assert live[0] == 403
        assert "s1" not in limit_continue._ARMED

    @pytest.mark.asyncio
    async def test_delete_cancels_the_wait_and_says_whether_there_was_one(self):
        slot = _slot("s1", [_limit_row(START + 3600)])
        state = _state(slot)
        limit_continue.arm(state, slot, START + 3600)
        assert await self._call(state, "DELETE", "s1") == (200, {"ok": True, "cancelled": True})
        assert "s1" not in limit_continue._ARMED
        assert await self._call(state, "DELETE", "s1") == (200, {"ok": True, "cancelled": False})

    @pytest.mark.asyncio
    async def test_the_routes_are_registered(self):
        from kiro_crew.dashboard.routes import chat as chat_routes

        app = web.Application()
        chat_routes.register(app)
        found = {
            (r.method, r.resource.canonical)
            for r in app.router.routes()
            if r.resource is not None and r.resource.canonical.endswith("/limit-continue")
        }
        assert found == {
            ("POST", "/api/chat/slots/{slot}/limit-continue"),
            ("DELETE", "/api/chat/slots/{slot}/limit-continue"),
        }
        assert limit_continue.cancel_all in app.on_cleanup


class TestTheSlotSummary:
    @pytest.mark.asyncio
    async def test_an_armed_chat_says_when_it_will_resume_and_others_are_unchanged(self, tmp_path):
        state = _make_state(tmp_path)
        armed_slot = state.get_or_create_slot("limit-armed")
        armed_slot.messages.append(_limit_row(START + 3600))
        idle_slot = state.get_or_create_slot("limit-idle")
        assert "limit_continue_at" not in state.serialize_slot(armed_slot)

        limit_continue.arm(state, armed_slot, START + 3600)
        assert state.serialize_slot(armed_slot)["limit_continue_at"] == _iso(START + 3660)
        assert "limit_continue_at" not in state.serialize_slot(idle_slot)
