"""The dashboard Stop must address the session the turn actually runs on.

A channel-linked slot's turns run under its ``linked_session_key``
(``slack:<ts>``), not under ``dashboard:<slot>``. Handing ``stop_turn`` the
slot-derived key names a session no running turn owns, so the cancel is a no-op
while the handler still inserts the stop card and answers ``{"ok": true}`` — the
operator is told the turn stopped and it keeps executing.

Each test asserts the KEY the handler passes, because that is the whole defect:
the surrounding card/state bookkeeping was already correct.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from body_stream_helpers import BodyStreamPayload

LINKED_KEY = "slack:1730000000.123456"

#: What both a missing slot and a refused caller must return, byte for byte.
MISSING_SLOT_BODY = {"error": "not found", "code": "slot_not_found"}


class _FakeSlot:
    """Minimal ChatSlot stand-in, optionally linked to a channel session."""

    def __init__(self, linked: str = "", app: str = ""):
        self._app = app
        # Runtime-only turn identity; empty until _run_chat installs one, which
        # is also what a slot rehydrated from disk answers.
        self._active_turn_session_key = ""
        self._stop_state = "idle"
        self._stop_event_id = None
        self._stop_escalated_card_id = None
        self._queue: list[dict] = []
        self._pending_steers: list[dict] = []
        self._auto_run = False
        self.running = True
        self.key = "test-slot"
        self.agent = "kirocrew"
        self.linked_session_key = linked
        self.messages: list[dict] = []
        self.source_links_invalidated = 0

    def append(self, role, content, cls_meta):
        self.messages.append({"role": role, "content": content, "cls": cls_meta})

    def invalidate_source_links(self):
        self.source_links_invalidated += 1

    def queue_insert(self, index, content, kind=""):
        self._queue.insert(index, {"content": content, "kind": kind})


class _FakeState:
    def __init__(self, slot):
        self._slots = {"test-slot": slot}
        self.sessions = MagicMock()
        self.sessions.stop_turn = AsyncMock(return_value="cancelled")

    def push_slots_update(self):
        pass

    def cancel_questions_for_slot(self, slot_key):
        return 0


def _request(state, query=None, caller_app="", raw: bytes = b""):
    app = web.Application()
    app["state"] = state
    request = MagicMock()
    request.app = app
    request.match_info = {"slot": "test-slot"}
    request.query = query or {}
    # The bounded body read consumes these instead of ``request.json``. An
    # empty ``raw`` reads as "no body sent", which the allow_absent handlers
    # default to an empty object.
    request.content = BodyStreamPayload(raw)
    request.content_length = len(raw)
    request.can_read_body = bool(raw)
    request.charset = None
    # The auth middleware stashes the calling app's name here; a dashboard user
    # leaves it absent, which `request.get("app", "")` reads as "".
    request.get = lambda key, default="": caller_app if key == "app" else default
    return request


def _stopped_key(state) -> str:
    """The session key the handler handed to ``stop_turn``."""
    assert state.sessions.stop_turn.await_count == 1, "stop_turn was not called exactly once"
    return state.sessions.stop_turn.await_args.args[0]


def api_chat_slot_stop_h():
    from kiro_crew.dashboard.chat_handlers import api_chat_slot_stop

    return api_chat_slot_stop


async def _run(handler, state, query=None, caller_app=""):
    with patch("kiro_crew.dashboard.chat_handlers.sel") as mock_sel:
        mock_sel.return_value.log_tool_invocation = MagicMock()
        mock_sel.return_value.log = MagicMock()
        mock_sel.return_value.log_api_access = MagicMock()
        with patch("kiro_crew.dashboard.chat_handlers._reject_pending_approvals"):
            return await handler(_request(state, query, caller_app))


class TestStopAddressesTheRunningSession:
    @pytest.mark.asyncio
    async def test_soft_stop_on_a_linked_slot_cancels_the_channel_session(self):
        from kiro_crew.dashboard.chat_handlers import api_chat_slot_stop

        state = _FakeState(_FakeSlot(linked=LINKED_KEY))
        await _run(api_chat_slot_stop, state)

        assert _stopped_key(state) == LINKED_KEY

    @pytest.mark.asyncio
    async def test_hard_kill_on_a_linked_slot_cancels_the_channel_session(self):
        """The escalation path is the one a user reaches when the soft stop did
        nothing, so it must not repeat the same mis-addressing."""
        from kiro_crew.dashboard.chat_handlers import api_chat_slot_stop

        slot = _FakeSlot(linked=LINKED_KEY)
        slot._stop_state = "soft_pending"
        slot._stop_event_id = "stop-abc"
        state = _FakeState(slot)

        await _run(api_chat_slot_stop, state, query={"force": "true"})

        assert _stopped_key(state) == LINKED_KEY

    @pytest.mark.asyncio
    async def test_interrupt_on_a_linked_slot_cancels_the_channel_session(self):
        from kiro_crew.dashboard.chat_handlers import api_chat_slot_interrupt

        # /interrupt refuses an empty queue (400, "use /stop instead"), so the
        # slot needs a queued message to reach the cancel at all.
        slot = _FakeSlot(linked=LINKED_KEY)
        slot._queue = [{"queue_id": "q1", "content": "next"}]
        state = _FakeState(slot)
        await _run(api_chat_slot_interrupt, state)

        assert _stopped_key(state) == LINKED_KEY


class TestUnlinkedSlotsAreUnchanged:
    """Preservation: an ordinary dashboard slot has no link, so
    ``effective_session_key`` falls back to exactly the previous key."""

    @pytest.mark.asyncio
    async def test_soft_stop_still_uses_the_dashboard_key(self):
        from kiro_crew.dashboard.chat_handlers import api_chat_slot_stop

        state = _FakeState(_FakeSlot())
        await _run(api_chat_slot_stop, state)

        assert _stopped_key(state) == "dashboard:test-slot"

    @pytest.mark.asyncio
    async def test_hard_kill_still_uses_the_dashboard_key(self):
        from kiro_crew.dashboard.chat_handlers import api_chat_slot_stop

        slot = _FakeSlot()
        slot._stop_state = "soft_pending"
        slot._stop_event_id = "stop-abc"
        state = _FakeState(slot)

        await _run(api_chat_slot_stop, state, query={"force": "true"})

        assert _stopped_key(state) == "dashboard:test-slot"

    @pytest.mark.asyncio
    async def test_interrupt_still_uses_the_dashboard_key(self):
        from kiro_crew.dashboard.chat_handlers import api_chat_slot_interrupt

        slot = _FakeSlot()
        slot._queue = [{"queue_id": "q1", "content": "next"}]
        state = _FakeState(slot)
        await _run(api_chat_slot_interrupt, state)

        assert _stopped_key(state) == "dashboard:test-slot"


class TestAppTokensCannotCancelForeignSlots:
    """App Kit §5.2 ownership, enforced before any side effect.

    Resolving the real session key is what makes this reachable: addressing
    `dashboard:<slot>` cancelled nothing, so the missing guard on /stop and
    /interrupt was inert. Now the cancel lands, and an app token holding
    `/api/chat/*` could otherwise name a foreign linked slot and kill the
    channel turn running on it.

    `_unblock_pending_waits`, the stop card, the `_stop_state` claim and the
    queue clear all happen before `stop_turn`, so "no side effect" is asserted
    on the slot as well as on the mock.
    """

    def _assert_untouched(self, resp, state, slot):
        assert resp.status == 404
        # Byte-identical to a missing slot, not merely the same status: a
        # differing `code` would let an app tell "not mine" from "not there"
        # and enumerate foreign slot names.
        assert json.loads(resp.body) == MISSING_SLOT_BODY
        assert state.sessions.stop_turn.await_count == 0
        assert slot.messages == [], "a stop card was written for a denied caller"
        assert slot._stop_state == "idle"
        assert slot._queue == []

    @pytest.mark.asyncio
    async def test_app_token_cannot_stop_a_dashboard_owned_linked_slot(self):
        from kiro_crew.dashboard.chat_handlers import api_chat_slot_stop

        slot = _FakeSlot(linked=LINKED_KEY)  # dashboard-owned: _app == ""
        state = _FakeState(slot)
        resp = await _run(api_chat_slot_stop, state, caller_app="rogue-app")
        self._assert_untouched(resp, state, slot)

    @pytest.mark.asyncio
    async def test_app_a_cannot_stop_app_bs_slot(self):
        from kiro_crew.dashboard.chat_handlers import api_chat_slot_stop

        slot = _FakeSlot(linked=LINKED_KEY, app="app-b")
        state = _FakeState(slot)
        resp = await _run(api_chat_slot_stop, state, caller_app="app-a")
        self._assert_untouched(resp, state, slot)

    @pytest.mark.asyncio
    async def test_the_hard_kill_path_cannot_bypass_the_guard(self):
        """The escalation branch clears the queue and drops pending steers
        before it reaches stop_turn, so the guard must precede it."""
        from kiro_crew.dashboard.chat_handlers import api_chat_slot_stop

        slot = _FakeSlot(linked=LINKED_KEY)
        slot._stop_state = "soft_pending"
        slot._queue = [{"queue_id": "q1", "content": "keep me"}]
        slot._pending_steers = [{"content": "keep me too"}]
        state = _FakeState(slot)

        resp = await _run(
            api_chat_slot_stop, state, query={"force": "true"}, caller_app="rogue-app"
        )

        assert resp.status == 404
        assert state.sessions.stop_turn.await_count == 0
        assert slot._queue == [{"queue_id": "q1", "content": "keep me"}]
        assert slot._pending_steers == [{"content": "keep me too"}]
        assert slot._stop_state == "soft_pending"

    @pytest.mark.asyncio
    async def test_app_token_cannot_interrupt_a_foreign_slot(self):
        from kiro_crew.dashboard.chat_handlers import api_chat_slot_interrupt

        slot = _FakeSlot(linked=LINKED_KEY)
        slot._queue = [{"queue_id": "q1", "content": "next"}]
        state = _FakeState(slot)

        resp = await _run(api_chat_slot_interrupt, state, caller_app="rogue-app")

        assert resp.status == 404
        assert state.sessions.stop_turn.await_count == 0
        assert slot._stop_state == "idle"
        assert slot.messages == []

    @pytest.mark.asyncio
    async def test_an_owned_but_channel_linked_slot_is_still_refused(self):
        """Slot ownership does not prove ownership of the linked session.

        ``get_or_create_slot`` takes ``app`` and, for a name shaped like a
        channel session stem, resolves ``linked_session_key`` from the session
        map in the SAME call — so an app that names a live channel thread ends
        up legitimately owning a slot bound to a conversation it has no claim
        on. An ownership-only guard authorizes cancelling that channel's turn,
        which turns the slot binding into capability escalation.
        """
        slot = _FakeSlot(linked=LINKED_KEY, app="app-a")
        state = _FakeState(slot)

        resp = await _run(api_chat_slot_stop_h(), state, caller_app="app-a")

        # Ownership alone would have allowed this.
        assert slot._app == "app-a"
        self._assert_untouched(resp, state, slot)

    @pytest.mark.asyncio
    async def test_an_owned_channel_linked_slot_is_refused_on_interrupt_too(self):
        from kiro_crew.dashboard.chat_handlers import api_chat_slot_interrupt

        slot = _FakeSlot(linked=LINKED_KEY, app="app-a")
        slot._queue = [{"queue_id": "q1", "content": "next"}]
        state = _FakeState(slot)

        resp = await _run(api_chat_slot_interrupt, state, caller_app="app-a")

        assert resp.status == 404
        assert json.loads(resp.body) == MISSING_SLOT_BODY
        assert state.sessions.stop_turn.await_count == 0
        assert slot._stop_state == "idle"
        assert slot.messages == []
        assert slot._queue == [{"queue_id": "q1", "content": "next"}]

    @pytest.mark.asyncio
    async def test_an_owned_channel_linked_slot_is_refused_on_hard_kill_too(self):
        slot = _FakeSlot(linked=LINKED_KEY, app="app-a")
        slot._stop_state = "soft_pending"
        slot._queue = [{"queue_id": "q1", "content": "keep me"}]
        slot._pending_steers = [{"content": "keep me too"}]
        state = _FakeState(slot)

        resp = await _run(
            api_chat_slot_stop_h(), state, query={"force": "true"}, caller_app="app-a"
        )

        assert resp.status == 404
        assert json.loads(resp.body) == MISSING_SLOT_BODY
        assert state.sessions.stop_turn.await_count == 0
        assert slot._queue == [{"queue_id": "q1", "content": "keep me"}]
        assert slot._pending_steers == [{"content": "keep me too"}]

    @pytest.mark.asyncio
    async def test_a_missing_slot_answers_exactly_like_a_refusal(self):
        """The oracle check from the other side: if these two bodies ever
        diverge, every denial above becomes an existence probe."""
        state = _FakeState(_FakeSlot())
        state._slots = {}  # no such slot

        resp = await _run(api_chat_slot_stop_h(), state, caller_app="rogue-app")

        assert resp.status == 404
        assert json.loads(resp.body) == MISSING_SLOT_BODY

    @pytest.mark.asyncio
    async def test_the_owning_app_may_still_stop_its_own_slot(self):
        """No app-owned slot carries a linked_session_key today (only
        auto-research and spec-builder pass ``app=``, neither with a link), so
        the owning-app case is exercised on the shape that actually exists. The
        rule is ownership, not linkage, matching api_chat_slot_continue."""
        from kiro_crew.dashboard.chat_handlers import api_chat_slot_stop

        slot = _FakeSlot(app="auto-research")
        state = _FakeState(slot)

        await _run(api_chat_slot_stop, state, caller_app="auto-research")

        assert _stopped_key(state) == "dashboard:test-slot"

    @pytest.mark.asyncio
    async def test_dashboard_user_is_not_treated_as_an_app(self):
        """Empty request app = dashboard user, who reaches every slot."""
        from kiro_crew.dashboard.chat_handlers import api_chat_slot_stop

        slot = _FakeSlot(linked=LINKED_KEY, app="some-app")
        state = _FakeState(slot)

        await _run(api_chat_slot_stop, state, caller_app="")

        assert _stopped_key(state) == LINKED_KEY


class TestAuditKeepsTheSlotIdentity:
    """The SEL record answers "which tab did the operator press", so it stays on
    the slot-derived key even when the session addressed is the channel's.
    ``api_chat_slot_continue`` already splits the two the same way."""

    @pytest.mark.asyncio
    async def test_sel_record_is_keyed_on_the_slot_not_the_link(self):
        from kiro_crew.dashboard.chat_handlers import api_chat_slot_stop

        state = _FakeState(_FakeSlot(linked=LINKED_KEY))
        with patch("kiro_crew.dashboard.chat_handlers.sel") as mock_sel:
            log = MagicMock()
            mock_sel.return_value.log_tool_invocation = log
            mock_sel.return_value.log = MagicMock()
            with patch("kiro_crew.dashboard.chat_handlers._reject_pending_approvals"):
                await api_chat_slot_stop(_request(state, caller_app=""))

        assert log.call_args.kwargs["session_key"] == "dashboard:test-slot"
        assert _stopped_key(state) == LINKED_KEY


class TestTheRunningTurnOwnsTheTarget:
    """A cancel addresses the turn in flight, not the slot's current routing.

    ``linked_session_key`` is mutable on a LIVE slot:
    ``inject_cron_result_to_dashboard`` binds an existing slot to ``cron:<id>``
    and does not gate on ``slot.running``. ``_run_chat`` captures its session
    key once and uses that one for the whole turn, so after a mid-turn rebind
    the two disagree — and a cancel that re-derives the key stops a session this
    turn never ran on while the real turn keeps executing.
    """

    @pytest.mark.asyncio
    async def test_soft_stop_after_a_mid_turn_rebind_stops_the_running_turn(self):
        from kiro_crew.dashboard.chat_handlers import api_chat_slot_stop

        slot = _FakeSlot()
        # The turn started on the slot's own session ...
        slot._active_turn_session_key = "dashboard:test-slot"
        # ... and a cron injection rebound the slot while it was still running.
        slot.linked_session_key = "cron:nightly-report"
        state = _FakeState(slot)

        await _run(api_chat_slot_stop, state)

        assert (
            _stopped_key(state) == "dashboard:test-slot"
        ), "the stop followed the slot's new routing instead of the running turn"

    @pytest.mark.asyncio
    async def test_the_hard_escalation_follows_the_same_turn(self):
        """The second press must not retarget where the first one could not."""
        from kiro_crew.dashboard.chat_handlers import api_chat_slot_stop

        slot = _FakeSlot()
        slot._active_turn_session_key = "dashboard:test-slot"
        slot.linked_session_key = "cron:nightly-report"
        slot._stop_state = "soft_pending"  # the first press already landed
        state = _FakeState(slot)

        await _run(api_chat_slot_stop, state, query={"force": "true"})

        assert _stopped_key(state) == "dashboard:test-slot"

    @pytest.mark.asyncio
    async def test_interrupt_after_a_mid_turn_rebind_stops_the_running_turn(self):
        from kiro_crew.dashboard.chat_handlers import api_chat_slot_interrupt

        slot = _FakeSlot()
        slot._active_turn_session_key = "dashboard:test-slot"
        slot.linked_session_key = "cron:nightly-report"
        slot._queue.append({"queue_id": "q1", "content": "next"})
        state = _FakeState(slot)
        request = _request(state, caller_app="")
        request.content_length = 0
        with patch("kiro_crew.dashboard.chat_handlers.sel") as mock_sel:
            mock_sel.return_value.log_tool_invocation = MagicMock()
            mock_sel.return_value.log = MagicMock()
            mock_sel.return_value.log_api_access = MagicMock()
            with patch("kiro_crew.dashboard.chat_handlers._reject_pending_approvals"):
                await api_chat_slot_interrupt(request)

        assert _stopped_key(state) == "dashboard:test-slot"

    @pytest.mark.asyncio
    async def test_a_turn_that_started_on_the_link_is_still_stopped_there(self):
        """The #2462 fix, restated on the stronger rule.

        A channel-born slot is bound before its turn starts, so the turn's own
        identity IS the channel session — the cancel reaches it because that is
        where the turn runs, not because the slot happens to be linked now.
        """
        from kiro_crew.dashboard.chat_handlers import api_chat_slot_stop

        slot = _FakeSlot(linked=LINKED_KEY)
        slot._active_turn_session_key = LINKED_KEY
        state = _FakeState(slot)

        await _run(api_chat_slot_stop, state)

        assert _stopped_key(state) == LINKED_KEY

    @pytest.mark.asyncio
    async def test_an_app_may_still_stop_its_own_turn_after_a_rebind(self):
        """Authorization reads the same target, so mutable routing cannot
        lock an app out of the turn it legitimately started."""
        from kiro_crew.dashboard.chat_handlers import api_chat_slot_stop

        slot = _FakeSlot(app="auto-research")
        slot._active_turn_session_key = "dashboard:test-slot"
        slot.linked_session_key = "cron:nightly-report"
        state = _FakeState(slot)

        await _run(api_chat_slot_stop, state, caller_app="auto-research")

        assert _stopped_key(state) == "dashboard:test-slot"

    @pytest.mark.asyncio
    async def test_an_app_is_still_refused_a_turn_running_on_a_foreign_session(self):
        """The security boundary is unchanged: the guard now tests the key the
        cancel will actually use, and a turn running on a channel session the
        app does not own is still refused."""
        from kiro_crew.dashboard.chat_handlers import api_chat_slot_stop

        slot = _FakeSlot(linked=LINKED_KEY, app="auto-research")
        slot._active_turn_session_key = LINKED_KEY
        state = _FakeState(slot)

        resp = await _run(api_chat_slot_stop, state, caller_app="auto-research")

        assert resp.status == 404
        assert json.loads(resp.body) == MISSING_SLOT_BODY
        assert state.sessions.stop_turn.await_count == 0

    @pytest.mark.asyncio
    async def test_an_idle_slot_falls_back_to_its_routing(self):
        """No turn in flight means no captured identity — and a slot restored
        from disk has none either, because the field is runtime-only."""
        from kiro_crew.dashboard.chat_handlers import api_chat_slot_stop

        slot = _FakeSlot(linked=LINKED_KEY)
        assert slot._active_turn_session_key == ""
        state = _FakeState(slot)

        await _run(api_chat_slot_stop, state)

        assert _stopped_key(state) == LINKED_KEY


class TestTheAuthorizedSessionCannotMoveMidFlight:
    """What the guard cleared is what gets cancelled.

    ``linked_session_key`` is assignable on a LIVE slot: promoting a cron to a
    tab (``POST /api/crons/{id}/to-chat``) binds an already-existing slot to
    ``cron:<id>``. ``/interrupt`` awaits the request body between the guard and
    ``stop_turn``, so a key re-read at the call site can name a session the
    guard never saw — the authorization and the action would be about two
    different conversations.
    """

    @pytest.mark.asyncio
    async def test_interrupt_cancels_the_session_the_guard_cleared(self):
        """The app owns an unbound slot; the guard allows it; the slot is bound
        during the body await. The cancel must still address the cleared key."""
        from kiro_crew.dashboard.chat_handlers import api_chat_slot_interrupt

        slot = _FakeSlot(app="auto-research")
        slot._queue.append({"queue_id": "q1", "content": "next"})
        state = _FakeState(slot)
        request = _request(state, caller_app="auto-research", raw=b"{}")

        class _BindingPayload(BodyStreamPayload):
            # Stands in for the concurrent to-chat handler resuming inside the
            # awaited body read; it only assigns the field, exactly as that
            # handler does.
            async def iter_chunked(self, n: int):
                slot.linked_session_key = "cron:nightly-report"
                async for chunk in super().iter_chunked(n):
                    yield chunk

        request.content = _BindingPayload(b"{}")
        with patch("kiro_crew.dashboard.chat_handlers.sel") as mock_sel:
            mock_sel.return_value.log_tool_invocation = MagicMock()
            mock_sel.return_value.log = MagicMock()
            mock_sel.return_value.log_api_access = MagicMock()
            with patch("kiro_crew.dashboard.chat_handlers._reject_pending_approvals"):
                await api_chat_slot_interrupt(request)

        assert (
            _stopped_key(state) == "dashboard:test-slot"
        ), "the cancel followed a binding applied after authorization"

    @pytest.mark.asyncio
    async def test_a_legitimately_linked_slot_is_still_addressed_by_its_link(self):
        """The snapshot must not degrade the fix this PR exists for."""
        from kiro_crew.dashboard.chat_handlers import api_chat_slot_interrupt

        slot = _FakeSlot(linked=LINKED_KEY)
        slot._queue.append({"queue_id": "q1", "content": "next"})
        state = _FakeState(slot)
        request = _request(state, caller_app="")
        request.content_length = 0
        with patch("kiro_crew.dashboard.chat_handlers.sel") as mock_sel:
            mock_sel.return_value.log_tool_invocation = MagicMock()
            mock_sel.return_value.log = MagicMock()
            mock_sel.return_value.log_api_access = MagicMock()
            with patch("kiro_crew.dashboard.chat_handlers._reject_pending_approvals"):
                await api_chat_slot_interrupt(request)

        assert _stopped_key(state) == LINKED_KEY
