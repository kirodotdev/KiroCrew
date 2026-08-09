"""Tests for the wait-countdown half of /api/session-keepalive.

The sleeping `wait` tool has exactly one inbound channel — its own keepalive
ping — so this handler carries two jobs that must not interfere: publishing an
in-flight countdown to the dashboard slot, and handing back an "end this wait"
request exactly once.

Covers ``_service_wait_ping`` directly (state transitions), the route wrapper
(body robustness), and ``_ChatSlot.to_dict``'s ``wait_state`` field.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import sessions as sessions_mod
from kiro_crew.dashboard.handlers.sessions import (
    _service_wait_ping,
    api_session_keepalive,
)
from kiro_crew.dashboard.state import DashboardState, _ChatSlot

SLOT = "test"


class _FakeSessions:
    """Same shape as test_session_keepalive.py's double: only "known" resolves."""

    def __init__(self, provider):
        self._provider = provider

    def get_provider(self, key):
        return self._provider if key == "known" else None


def _mock_state(slot: _ChatSlot | None = None) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state._slots = {}
    if slot is not None:
        state._slots[slot.key] = slot
    state.get_slot = MagicMock(side_effect=lambda name: state._slots.get(name))
    state.push_slots_update = MagicMock()
    state.sessions = MagicMock()
    return state


def _ping(state, body: dict, *, session_key: str = "known", tab: str = SLOT) -> dict:
    """Invoke _service_wait_ping the way api_session_keepalive does.

    ``dashboard_slot_key`` is patched rather than seeded through the surface
    registry: the mapping from session key to open tab is that function's
    contract, not this handler's, and ``tab=""`` is the only way to express
    "this session has no dashboard tab".
    """
    reply: dict = {"ok": True}
    wait_id = str(body.get("wait_id") or "").strip()[:64]
    with patch(
        "kiro_crew.dashboard.chat_utils.dashboard_slot_key", return_value=tab
    ) as slot_key:
        _service_wait_ping(state, session_key, wait_id, body, reply)
    slot_key.assert_called_once_with(session_key)
    return reply


class _Clock:
    """Advanceable stand-in for the ``time`` module `_service_wait_ping` reads.

    Per-test, never module-level: these tests run under pytest-randomly and
    xdist, so a shared mutable clock would make one test's advance visible to
    whichever test happened to be ordered after it. ``now`` is read through the
    method, so an advance after patching is seen by the code under test.
    """

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def time(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


class TestWaitStateRecording:
    def test_first_ping_records_wait_state(self):
        """A. First sight of a wait_id mints wait_id + seconds + a deadline on
        the dashboard's own clock, and publishes it to the tab."""
        slot = _ChatSlot(SLOT)
        state = _mock_state(slot)

        before = time.time()
        reply = _ping(state, {"wait_id": "w1", "seconds": 300, "remaining": 297})
        after = time.time()

        assert reply == {"ok": True}  # nothing to hand back yet
        assert slot._wait_state is not None
        assert slot._wait_state["wait_id"] == "w1"
        assert slot._wait_state["seconds"] == 300
        # deadline_ts is time.time() + remaining, derived once from the tool's
        # own remaining budget (the two clocks share no epoch).
        assert before + 297 <= slot._wait_state["deadline_ts"] <= after + 297
        assert slot._end_wait_request is None
        state.push_slots_update.assert_called_once_with()

    def test_second_ping_same_id_does_not_remint_deadline(self):
        """B. Re-minting every ping would make the countdown jitter by one
        round-trip per tick, so the deadline is frozen on first sight."""
        slot = _ChatSlot(SLOT)
        state = _mock_state(slot)
        clock = SimpleNamespace(time=lambda: 1000.0)

        with patch.object(sessions_mod, "time", clock):
            _ping(state, {"wait_id": "w1", "seconds": 300, "remaining": 300})
            first = dict(slot._wait_state)
            assert first["deadline_ts"] == 1300.0
            # 30s of wall clock and 30s of the tool's budget later.
            clock.time = lambda: 1030.0
            _ping(state, {"wait_id": "w1", "seconds": 300, "remaining": 270})

        assert slot._wait_state == first  # deadline_ts unmoved at 1300.0
        # Nothing changed, so the tab is not re-pushed on every 5s ping.
        state.push_slots_update.assert_called_once_with()

    def test_new_wait_id_replaces_state_and_clears_stale_end_request(self):
        """C. A brand-new sleep cannot inherit an end request aimed at the
        previous one — that is what would make the next wait return instantly.

        The hand-over is only legitimate once the incumbent has STOPPED pinging:
        while it is still ticking, a second wait_id means two sleeps share this
        slot, which is the collision case below. So the clock is advanced past the
        liveness window here to model a previous sleep that really is gone (a
        missed wait_done, a killed MCP process).
        """
        slot = _ChatSlot(SLOT)
        state = _mock_state(slot)
        clk = _Clock()

        with patch.object(sessions_mod, "time", clk):
            _ping(state, {"wait_id": "old", "seconds": 300, "remaining": 300})
            old_state = dict(slot._wait_state)
            slot._end_wait_request = "old"  # user clicked End, tool never collected
            # 2.5 * 5s interval is the window; 20s is comfortably past it.
            clk.advance(20.0)
            reply = _ping(state, {"wait_id": "new", "seconds": 60, "remaining": 60})

        assert slot._wait_state["wait_id"] == "new"
        assert slot._wait_state["seconds"] == 60
        assert slot._wait_state["deadline_ts"] != old_state["deadline_ts"]
        assert slot._end_wait_request is None
        assert "end_wait" not in reply  # the new wait is NOT ended by it
        assert state.push_slots_update.call_count == 2

    def test_two_live_waits_on_one_slot_suppress_the_countdown(self):
        """C2. The ambiguous-identity guard.

        `_resolve_session_key()` answers per RUNTIME: with the MCP gateway off
        (the default) a subagent's `wait` and its parent's resolve to the SAME key
        and land here on one slot. Taking the newer one over would attribute one
        sleep's countdown to the other's pill and hand the user's End-wait click
        to whichever polled next. Since the ping doubles as a heartbeat, a second
        wait_id arriving while the incumbent is STILL pinging is provable
        ambiguity — so neither is tracked and the row disappears.
        """
        slot = _ChatSlot(SLOT)
        state = _mock_state(slot)
        clk = _Clock()

        with patch.object(sessions_mod, "time", clk):
            _ping(state, {"wait_id": "parent", "seconds": 300, "remaining": 300})
            assert slot._wait_state["wait_id"] == "parent"
            # The subagent's sleep pings 1s later — well inside the window, so the
            # parent's sleep is demonstrably still running.
            clk.advance(1.0)
            reply = _ping(state, {"wait_id": "sub", "seconds": 120, "remaining": 120})

            assert slot._wait_state is None, "neither wait may be attributed"
            assert "end_wait" not in reply
            assert slot._wait_contested is True

            # Both keep pinging. Neither is ever minted while contested, so no
            # countdown can appear for either.
            for _ in range(3):
                clk.advance(1.0)
                assert "end_wait" not in _ping(
                    state, {"wait_id": "parent", "seconds": 300, "remaining": 290}
                )
                assert "end_wait" not in _ping(
                    state, {"wait_id": "sub", "seconds": 120, "remaining": 110}
                )
                assert slot._wait_state is None

            # A click that raced the collision cannot be delivered either.
            slot._end_wait_request = "parent"
            assert "end_wait" not in _ping(
                state, {"wait_id": "parent", "seconds": 300, "remaining": 285}
            )
            assert slot._end_wait_request is None

            # The latch does NOT expire on a timer, and this is the whole point.
            # An earlier revision released it after one window; both sleeps were
            # still pinging, so whichever pinged first re-minted its own state and
            # published its deadline onto the other's pill for up to one interval,
            # with a live button that would end the wrong sleep. Advancing far past
            # any plausible window must change nothing.
            # Assert after EVERY INDIVIDUAL ping, never after a pair. The defect a
            # self-expiring window reintroduces is transient by nature: the first
            # ping after expiry publishes its own wait_state, and the very next
            # ping re-detects the collision and clears it again. Checking only
            # after both pings steps straight over the window in which the wrong
            # deadline was live and the button was armed — it is the difference
            # between this test catching the regression and rubber-stamping it.
            clk.advance(600.0)
            for _ in range(4):
                for wid, total, rem in (("sub", 120, 60), ("parent", 300, 200)):
                    clk.advance(5.0)
                    assert "end_wait" not in _ping(
                        state, {"wait_id": wid, "seconds": total, "remaining": rem}
                    )
                    assert slot._wait_state is None, (
                        f"latch must not expire on a clock: {wid} published "
                        f"{slot._wait_state!r} after the window elapsed"
                    )
            assert slot._wait_contested is True

    def test_turn_end_is_what_releases_the_contested_latch(self):
        """C2b. Turn end is the ONLY release, so a slot that saw colliding sleeps
        gets its countdown back on the next turn rather than never again."""
        slot = _ChatSlot(SLOT)
        state = _mock_state(slot)
        clk = _Clock()

        with patch.object(sessions_mod, "time", clk):
            _ping(state, {"wait_id": "a", "seconds": 300, "remaining": 300})
            clk.advance(1.0)
            _ping(state, {"wait_id": "b", "seconds": 300, "remaining": 300})
            assert slot._wait_contested is True

            # What chat_runner's turn-end block does (asserted against the real
            # fields rather than by importing the runner, whose block sits inside
            # a very large function).
            slot._wait_state = None
            slot._end_wait_request = None
            slot._wait_contested = False

            clk.advance(5.0)
            _ping(state, {"wait_id": "c", "seconds": 120, "remaining": 120})
            assert slot._wait_state["wait_id"] == "c"

    def test_contested_guard_does_not_fire_for_a_single_wait(self):
        """C3. The guard must not cost the normal case anything: one sleep
        pinging every interval for minutes is never contested."""
        slot = _ChatSlot(SLOT)
        state = _mock_state(slot)
        clk = _Clock()

        with patch.object(sessions_mod, "time", clk):
            for i in range(40):
                _ping(
                    state,
                    {"wait_id": "solo", "seconds": 300, "remaining": 300 - i * 5},
                )
                clk.advance(5.0)

        assert slot._wait_state["wait_id"] == "solo"
        assert slot._wait_contested is False
        # Minted once, pushed once: the 39 heartbeats are silent.
        state.push_slots_update.assert_called_once_with()

    @pytest.mark.parametrize(
        "remaining,seconds,want_remaining,want_seconds",
        [
            (None, None, 0, 0),  # absent -> 0
            (99999, 99999, 1800, 1800),  # clamped to the tool's own ceiling
            (-5, -5, 0, 0),  # clamped at the floor
            ("300", "600", 300, 600),  # numeric strings still parse
            (12.9, 300, 12, 300),  # truncated, not rounded
        ],
    )
    def test_remaining_and_seconds_are_clamped_not_trusted(
        self, remaining, seconds, want_remaining, want_seconds
    ):
        """The body is agent-reachable, so both numbers are bounded before they
        reach a countdown the browser renders."""
        slot = _ChatSlot(SLOT)
        state = _mock_state(slot)
        clock = SimpleNamespace(time=lambda: 500.0)

        with patch.object(sessions_mod, "time", clock):
            _ping(
                state,
                {"wait_id": "w1", "seconds": seconds, "remaining": remaining},
            )

        assert slot._wait_state["seconds"] == want_seconds
        assert slot._wait_state["deadline_ts"] == 500.0 + want_remaining

    @pytest.mark.parametrize(
        "bad", ["not-a-number", object(), 1j], ids=["str", "object", "complex"]
    )
    def test_one_unparseable_number_zeroes_both(self, bad):
        """The two parses share one try/except, so a bad ``remaining`` also
        zeroes an otherwise-valid ``seconds``. Documented rather than worked
        around: both fields come from the same ping, so a body that is half
        garbage has nothing worth salvaging, and 0/0 renders as "no countdown"
        instead of a wrong one.
        """
        slot = _ChatSlot(SLOT)
        state = _mock_state(slot)
        clock = SimpleNamespace(time=lambda: 500.0)

        with patch.object(sessions_mod, "time", clock):
            _ping(state, {"wait_id": "w1", "seconds": 300, "remaining": bad})

        assert slot._wait_state["seconds"] == 0
        assert slot._wait_state["deadline_ts"] == 500.0
        # Still recorded, so the "End wait" button has an id to quote and the
        # tool's own wait_done ping can still retire it.
        assert slot._wait_state["wait_id"] == "w1"

    @pytest.mark.parametrize("falsy", [[], {}, "", 0, False], ids=list("abcde"))
    def test_falsy_remaining_short_circuits_to_zero_without_raising(self, falsy):
        """``or 0`` runs before ``int()``, so an empty container never reaches
        the parse and cannot take the valid ``seconds`` down with it."""
        slot = _ChatSlot(SLOT)
        state = _mock_state(slot)
        clock = SimpleNamespace(time=lambda: 500.0)

        with patch.object(sessions_mod, "time", clock):
            _ping(state, {"wait_id": "w1", "seconds": 300, "remaining": falsy})

        assert slot._wait_state["seconds"] == 300
        assert slot._wait_state["deadline_ts"] == 500.0


class TestEndWaitHandoff:
    def test_end_request_is_handed_back_and_consumed_once(self):
        """D. The parked request reaches the tool on its next poll, and both the
        flag and the countdown retire in the same step."""
        slot = _ChatSlot(SLOT)
        state = _mock_state(slot)

        _ping(state, {"wait_id": "w1", "seconds": 300, "remaining": 300})
        # api_chat_slot_end_wait only ever writes the flag while _wait_state
        # already names this exact wait (409 otherwise), so that is the state
        # the handoff must be exercised from.
        slot._end_wait_request = "w1"

        reply = _ping(state, {"wait_id": "w1", "seconds": 300, "remaining": 290})

        assert reply["end_wait"] == "w1"
        assert slot._end_wait_request is None
        assert slot._wait_state is None
        assert state.push_slots_update.call_count == 2

        # Consume exactly once: a second poll must not re-deliver it.
        again = _ping(state, {"wait_id": "w1", "seconds": 300, "remaining": 285})
        assert "end_wait" not in again

    def test_end_request_without_matching_state_is_dropped_by_the_mint(self):
        """A flag with no live countdown behind it is unreachable through the
        route, and is dropped rather than applied to whatever sleeps next."""
        slot = _ChatSlot(SLOT)
        state = _mock_state(slot)
        slot._end_wait_request = "w1"  # no _wait_state: only reachable by hand

        reply = _ping(state, {"wait_id": "w1", "seconds": 300, "remaining": 300})

        assert "end_wait" not in reply
        assert slot._end_wait_request is None
        assert slot._wait_state["wait_id"] == "w1"

    def test_stale_end_request_for_another_wait_is_not_delivered(self):
        """E. A request naming a different sleep is neither delivered nor
        cleared — it is simply not ours to act on."""
        slot = _ChatSlot(SLOT)
        state = _mock_state(slot)

        _ping(state, {"wait_id": "mine", "seconds": 300, "remaining": 300})
        slot._end_wait_request = "other"

        reply = _ping(state, {"wait_id": "mine", "seconds": 300, "remaining": 295})

        assert "end_wait" not in reply
        assert slot._end_wait_request == "other"
        assert slot._wait_state["wait_id"] == "mine"
        state.push_slots_update.assert_called_once_with()  # mint only


class TestWaitDone:
    def test_wait_done_clears_state_owned_by_the_same_id(self):
        """F (positive half). The owning wait retires its own countdown."""
        slot = _ChatSlot(SLOT)
        state = _mock_state(slot)
        _ping(state, {"wait_id": "w1", "seconds": 300, "remaining": 300})
        slot._end_wait_request = "w1"

        reply = _ping(state, {"wait_id": "w1", "wait_done": True})

        assert reply == {"ok": True}
        assert slot._wait_state is None
        assert slot._end_wait_request is None
        assert state.push_slots_update.call_count == 2

    def test_wait_done_for_a_different_id_leaves_state_intact(self):
        """F. A late final ping from a previous sleep must not blank the
        countdown of the one now in flight."""
        slot = _ChatSlot(SLOT)
        state = _mock_state(slot)
        _ping(state, {"wait_id": "w1", "seconds": 300, "remaining": 300})
        live = dict(slot._wait_state)
        slot._end_wait_request = "w1"

        reply = _ping(state, {"wait_id": "stale-previous", "wait_done": True})

        assert reply == {"ok": True}
        assert slot._wait_state == live
        assert slot._end_wait_request == "w1"
        state.push_slots_update.assert_called_once_with()  # mint only

    def test_wait_done_does_not_mint_state_for_an_unseen_id(self):
        """wait_done is a retirement, never a registration."""
        slot = _ChatSlot(SLOT)
        state = _mock_state(slot)

        _ping(state, {"wait_id": "never-seen", "wait_done": True, "remaining": 60})

        assert slot._wait_state is None
        state.push_slots_update.assert_not_called()


class TestNoDashboardTab:
    def test_session_without_a_tab_is_a_silent_noop(self):
        """G. Slack/cron sessions can call `wait` too — they just have nothing
        to render a countdown on."""
        state = _mock_state()
        reply: dict = {"ok": True}

        with patch(
            "kiro_crew.dashboard.chat_utils.dashboard_slot_key", return_value=""
        ):
            _service_wait_ping(
                state,
                "slack:1785370133.085469",
                "w1",
                {"wait_id": "w1", "seconds": 300, "remaining": 300},
                reply,
            )

        assert reply == {"ok": True}
        state.get_slot.assert_not_called()
        state.push_slots_update.assert_not_called()

    def test_named_tab_that_no_longer_exists_is_a_silent_noop(self):
        """The tab closed mid-wait: the mapping still answers, the slot is gone."""
        state = _mock_state()  # empty _slots
        reply = _ping(state, {"wait_id": "w1", "remaining": 300}, tab="closed-tab")

        assert reply == {"ok": True}
        state.get_slot.assert_called_once_with("closed-tab")
        state.push_slots_update.assert_not_called()


def _keepalive_app(state: DashboardState) -> web.Application:
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/session-keepalive", api_session_keepalive)
    return app


class TestKeepaliveRouteBody:
    """H. The body is advisory; losing it must never cost the session its
    keepalive, because that is the half that keeps the staleness watchdog from
    SIGTERM'ing the ACP subprocess mid-wait."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "raw",
        [
            b"{not json",
            b"",
            json.dumps([1, 2, 3]).encode(),  # JSON, but not an object
            json.dumps("wait_id").encode(),
            b"null",
            b"5",  # truthy non-object: parses, has no .get
            b"true",
        ],
        ids=[
            "invalid-json",
            "empty",
            "json-list",
            "json-string",
            "json-null",
            "json-number",
            "json-true",
        ],
    )
    async def test_unusable_body_still_touches_and_returns_200(self, raw):
        provider = MagicMock()
        state = MagicMock(spec=DashboardState)
        state.sessions = _FakeSessions(provider=provider)

        with patch.object(sessions_mod, "_service_wait_ping") as serviced:
            async with TestClient(TestServer(_keepalive_app(state))) as client:
                resp = await client.post(
                    "/api/session-keepalive",
                    data=raw,
                    headers={
                        "X-Session-Key": "known",
                        "Content-Type": "application/json",
                    },
                )
                assert resp.status == 200
                assert await resp.json() == {"ok": True}

        provider.touch_activity.assert_called_once_with()
        # No wait_id survived the parse, so the countdown path stays out of it.
        serviced.assert_not_called()

    @pytest.mark.asyncio
    async def test_body_with_wait_id_reaches_the_countdown_path(self):
        """The wiring: a usable body is forwarded and its reply mutation is what
        the tool actually receives over HTTP."""
        provider = MagicMock()
        state = MagicMock(spec=DashboardState)
        state.sessions = _FakeSessions(provider=provider)

        def _fake(state_arg, session_key, wait_id, body, reply):
            reply["end_wait"] = wait_id

        with patch.object(sessions_mod, "_service_wait_ping", side_effect=_fake) as f:
            async with TestClient(TestServer(_keepalive_app(state))) as client:
                resp = await client.post(
                    "/api/session-keepalive",
                    json={"wait_id": "w1", "seconds": 300, "remaining": 297},
                    headers={"X-Session-Key": "known"},
                )
                assert resp.status == 200
                assert await resp.json() == {"ok": True, "end_wait": "w1"}

        provider.touch_activity.assert_called_once_with()
        args = f.call_args.args
        assert args[1] == "known"
        assert args[2] == "w1"
        assert args[3]["remaining"] == 297

    @pytest.mark.asyncio
    async def test_oversized_wait_id_is_truncated_before_use(self):
        state = MagicMock(spec=DashboardState)
        state.sessions = _FakeSessions(provider=MagicMock())

        with patch.object(sessions_mod, "_service_wait_ping") as serviced:
            async with TestClient(TestServer(_keepalive_app(state))) as client:
                resp = await client.post(
                    "/api/session-keepalive",
                    json={"wait_id": "x" * 500},
                    headers={"X-Session-Key": "known"},
                )
                assert resp.status == 200

        assert serviced.call_args.args[2] == "x" * 64

    @pytest.mark.asyncio
    async def test_whitespace_only_wait_id_is_not_a_wait_ping(self):
        state = MagicMock(spec=DashboardState)
        state.sessions = _FakeSessions(provider=MagicMock())

        with patch.object(sessions_mod, "_service_wait_ping") as serviced:
            async with TestClient(TestServer(_keepalive_app(state))) as client:
                resp = await client.post(
                    "/api/session-keepalive",
                    json={"wait_id": "   ", "wait_done": True},
                    headers={"X-Session-Key": "known"},
                )
                assert resp.status == 200
                assert await resp.json() == {"ok": True}

        serviced.assert_not_called()


class TestSlotToDict:
    """J. wait_state rides the slots payload so a page reload mid-wait
    re-seeds the countdown from GET /api/chat/slots for free."""

    def test_wait_state_is_none_when_idle(self):
        slot = _ChatSlot(SLOT)
        slot.messages.append({"role": "assistant", "content": "Done.", "ts": "t1"})

        d = slot.to_dict()

        assert "wait_state" in d
        assert d["wait_state"] is None

    def test_wait_state_is_emitted_when_a_wait_is_in_flight(self):
        slot = _ChatSlot(SLOT)
        slot.messages.append({"role": "assistant", "content": "Sleeping.", "ts": "t1"})
        payload = {"wait_id": "w1", "seconds": 300, "deadline_ts": 1234.5}
        slot._wait_state = payload

        d = slot.to_dict()

        assert d["wait_state"] == payload
        # The "End wait" button has to quote the id, so it must survive intact.
        assert d["wait_state"]["wait_id"] == "w1"

    def test_end_wait_request_is_not_exposed_to_the_client(self):
        """The parked request is a server-side handoff, not slot state the tab
        should render or echo back."""
        slot = _ChatSlot(SLOT)
        slot._wait_state = {"wait_id": "w1", "seconds": 60, "deadline_ts": 1.0}
        slot._end_wait_request = "w1"

        d = slot.to_dict()

        assert "end_wait_request" not in d
        assert "_end_wait_request" not in d
