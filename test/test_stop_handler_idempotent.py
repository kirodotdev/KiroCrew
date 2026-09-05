"""Idempotency + orphaned-stop-card regression tests for the dashboard stop /
interrupt handlers (provider-agnostic — ported from the upstream project,
defect 3). The CC-provider-specific classes in the upstream file are dropped:
KiroCrew is KiroACP-only and providers/claude_code.py does not exist here."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from body_stream_helpers import BodyStreamPayload


class _FakeSlot:
    """Minimal ChatSlot stand-in for handler tests."""

    def __init__(self):
        self._stop_state = "idle"
        self._stop_event_id = None
        self._stop_escalated_card_id = None
        self._queue: list[dict] = []
        self._auto_run = False
        self.running = True
        self.key = "test-slot"
        #: Set on every slot whose turns run on a session it did not name
        #: itself — a cron-born tab (``cron:<job_id>``), a channel-born tab
        #: (``slack:<ts>``), a workflow-born tab. Empty for a plain chat tab.
        self.linked_session_key = ""
        #: No owning app: the App Kit §5.2 cancel guard reads this, and a slot
        #: nobody owns is cancellable by the dashboard caller.
        self._app = None
        self._active_turn_session_key = ""
        #: Remote-execution binding — "local" so these tests exercise the LOCAL
        #: stop path. ``stop_slot_turn`` reads it to decide whether the stop must
        #: travel to a peer crew, and the property below mirrors ``_ChatSlot`` in
        #: requiring the WHOLE binding rather than just the marker.
        self.executor = "local"
        self.instance_id = ""
        self.remote_slot = ""
        self.agent = "kirocrew"
        self.messages: list[dict] = []
        self._dirty = False
        self.source_links_invalidated = 0

    @property
    def is_remote(self) -> bool:
        return bool(self.executor == "remote" and self.instance_id and self.remote_slot)

    def append(self, role, content, cls_meta):
        self.messages.append({"role": role, "content": content, "cls": cls_meta})

    def invalidate_source_links(self):
        self.source_links_invalidated += 1


class _FakeState:
    """Minimal DashboardState stand-in."""

    def __init__(self, slot):
        self._slots = {"test-slot": slot}
        self.sessions = MagicMock()
        self.sessions.stop_turn = AsyncMock(return_value="idle")
        self._push_count = 0

    def push_slots_update(self):
        self._push_count += 1

    def cancel_questions_for_slot(self, slot_key):
        """No pending ask_question cards in this fixture.

        Present because the stop path releases BOTH blocking waits (approvals
        and agent questions) through `_unblock_pending_waits`.
        """
        return 0


class TestStopHandlerIdempotent:
    """Repeat /stop press returns info without creating another card."""

    @pytest.mark.asyncio
    async def test_repeat_stop_no_new_card(self):
        """Second non-force stop press while soft_pending returns info."""
        from aiohttp import web

        from kiro_crew.dashboard.chat_handlers import api_chat_slot_stop

        slot = _FakeSlot()
        # Simulate a stop already in progress (first press completed the guard
        # at line 727 and would reach the escalation path, but the escalation
        # path only fires when _stop_state == "soft_pending". We test the new
        # idempotent guard for states like "killing".)
        slot._stop_state = "killing"
        slot._stop_event_id = "stop-abc"
        slot.running = True

        state = _FakeState(slot)
        app = web.Application()
        app["state"] = state

        request = MagicMock()
        # A bare MagicMock answers .get("app") with a truthy mock, which the
        # App Kit 5.2 ownership guard would read as an app token. These cases
        # are dashboard-user presses, so model the absent header explicitly.
        request.get = lambda key, default="": default
        request.app = app
        request.match_info = {"slot": "test-slot"}
        request.query = {}  # no force flag

        resp = await api_chat_slot_stop(request)
        body = json.loads(resp.body)

        assert body.get("info") == "stop already in progress"
        # No new messages appended (no new card created)
        assert len(slot.messages) == 0

    @pytest.mark.asyncio
    async def test_idle_outcome_resolves_card(self):
        """When stop_turn returns 'idle', the stop card is resolved."""
        from aiohttp import web

        from kiro_crew.dashboard.chat_handlers import api_chat_slot_stop

        slot = _FakeSlot()
        slot.running = True
        state = _FakeState(slot)
        state.sessions.stop_turn = AsyncMock(return_value="idle")

        app = web.Application()
        app["state"] = state

        request = MagicMock()
        # A bare MagicMock answers .get("app") with a truthy mock, which the
        # App Kit 5.2 ownership guard would read as an app token. These cases
        # are dashboard-user presses, so model the absent header explicitly.
        request.get = lambda key, default="": default
        request.app = app
        request.match_info = {"slot": "test-slot"}
        request.query = {}

        # Mock SEL logging and _reject_pending_approvals
        with patch("kiro_crew.dashboard.chat_handlers.sel") as mock_sel:
            mock_sel.return_value.log_tool_invocation = MagicMock()
            mock_sel.return_value.log = MagicMock()
            with patch("kiro_crew.dashboard.chat_handlers._reject_pending_approvals"):
                await api_chat_slot_stop(request)

        # After the handler, stop state should be back to idle and event_id cleared
        assert slot._stop_state == "idle"
        assert slot._stop_event_id is None
        assert slot.source_links_invalidated == 1


class TestInterruptHandlerIdempotent:
    """Repeat /interrupt press returns info without creating another card."""

    @pytest.mark.asyncio
    async def test_repeat_interrupt_no_new_card(self):
        """Interrupt while already stopping returns info."""
        from aiohttp import web

        from kiro_crew.dashboard.chat_handlers import api_chat_slot_interrupt

        slot = _FakeSlot()
        slot._stop_state = "soft_pending"
        slot._stop_event_id = "stop-xyz"
        slot.running = True
        slot._queue = [{"id": "q1", "content": "hello"}]

        state = _FakeState(slot)
        app = web.Application()
        app["state"] = state

        request = MagicMock()
        # A bare MagicMock answers .get("app") with a truthy mock, which the
        # App Kit 5.2 ownership guard would read as an app token. These cases
        # are dashboard-user presses, so model the absent header explicitly.
        request.get = lambda key, default="": default
        request.app = app
        request.match_info = {"slot": "test-slot"}
        request.content_length = 0

        resp = await api_chat_slot_interrupt(request)
        body = json.loads(resp.body)

        assert body.get("info") == "stop already in progress"
        # Queue unchanged
        assert len(slot._queue) == 1

    @pytest.mark.asyncio
    async def test_refused_body_restores_auto_run(self):
        """A 400-refused body rolls back BOTH claimed fields.

        The handler claims ``_stop_state`` and disables ``_auto_run`` before
        the body await; a request refused by the body guard must restore both,
        or a malformed /interrupt permanently disables orchestrator auto-run
        without interrupting anything.
        """
        from aiohttp import web

        from kiro_crew.dashboard.chat_handlers import api_chat_slot_interrupt

        slot = _FakeSlot()
        slot.running = True
        slot._queue = [{"id": "q1", "content": "hello"}]
        slot._auto_run = True

        state = _FakeState(slot)
        app = web.Application()
        app["state"] = state

        request = MagicMock()
        request.get = lambda key, default="": default
        request.app = app
        request.match_info = {"slot": "test-slot"}
        raw = b'["not", "an", "object"]'
        request.content = BodyStreamPayload(raw)
        request.content_length = len(raw)
        request.can_read_body = True
        request.charset = None

        resp = await api_chat_slot_interrupt(request)

        assert resp.status == 400
        assert slot._stop_state == "idle"
        assert slot._auto_run is True

    @pytest.mark.asyncio
    async def test_refused_body_does_not_erase_a_concurrent_hard_stop(self):
        """A rollback must not overwrite a stop escalated during the body await.

        The handler claims ``_stop_state = "soft_pending"`` before awaiting the
        body. A concurrent /stop landing during that await escalates the state
        (e.g. to ``"killing"``). When the body is then refused, rolling back to
        ``"idle"`` would erase the escalation and admit another stop while the
        hard kill still runs -- so the rollback fires only while the handler's
        own claim is intact, and the escalated stop keeps ``_auto_run`` too.
        """
        from aiohttp import web

        from kiro_crew.dashboard.chat_handlers import api_chat_slot_interrupt

        slot = _FakeSlot()
        slot.running = True
        slot._queue = [{"id": "q1", "content": "hello"}]
        slot._auto_run = True

        class EscalatingPayload(BodyStreamPayload):
            """Body stream that simulates a concurrent /stop mid-read."""

            async def iter_chunked(self, n: int):
                slot._stop_state = "killing"
                async for chunk in super().iter_chunked(n):
                    yield chunk

        state = _FakeState(slot)
        app = web.Application()
        app["state"] = state

        request = MagicMock()
        request.get = lambda key, default="": default
        request.app = app
        request.match_info = {"slot": "test-slot"}
        raw = b'["not", "an", "object"]'
        request.content = EscalatingPayload(raw)
        request.content_length = len(raw)
        request.can_read_body = True
        request.charset = None

        resp = await api_chat_slot_interrupt(request)

        assert resp.status == 400
        assert slot._stop_state == "killing"
        assert slot._auto_run is False


def _seed_stop_card(slot, stop_id="stop-race"):
    """Append an unresolved stop_event card, as the /stop handler does."""
    data = {"kind": "stop_event", "id": stop_id, "state": "stopping", "outcome": None}
    payload = json.dumps(data)
    slot.append("system", payload, payload)
    slot._stop_event_id = stop_id
    return stop_id


def _card_state(slot, stop_id):
    """Read the current state of the seeded stop_event card."""
    for msg in slot.messages:
        cls_data = json.loads(msg["cls"])
        if cls_data.get("kind") == "stop_event" and cls_data.get("id") == stop_id:
            return cls_data["state"]
    raise AssertionError(f"no stop_event card for {stop_id}")


class TestStopCardTeardownRace:
    """A turn tearing down must not strand the stop card at "stopping".

    `_finish_queue_cycle` (chat_runner.py) drives `_stopping = False` when the
    queue drains, which the setter in state.py maps to `_stop_state = "idle"`.
    That write races the soft-stop budget: when it landed before the escalation
    callback ran, the callback's old `_stop_state` gate bailed and the card was
    never settled. Observed against a live gateway as a stop card pulsing for
    40+ seconds. The resolver is now keyed on `_stop_event_id` instead.
    """

    def test_stopping_setter_clears_an_in_flight_stop_state(self):
        """Pin the prod write that creates the race.

        This is the mechanism the handler tests below simulate. If this ever
        stops mapping falsy to "idle", those simulations are no longer faithful.
        """
        from kiro_crew.dashboard.state import _ChatSlot

        slot = _ChatSlot("race-slot")
        slot._stop_state = "soft_pending"
        slot._stopping = False
        assert slot._stop_state == "idle"

        slot._stop_state = "killing"
        slot._stopping = False
        assert slot._stop_state == "idle"

    @pytest.mark.asyncio
    async def test_hard_escalation_resolves_card_after_teardown_reset(self):
        """Escalation settles the card even when teardown won the race."""
        from aiohttp import web

        from kiro_crew.dashboard.chat_handlers import api_chat_slot_stop

        slot = _FakeSlot()
        slot.running = True
        state = _FakeState(slot)

        async def _stop_turn(_key, force=False, preserve_queue=False, on_soft=None, on_hard=None):
            # The budget expires, then the dying turn's _finish_queue_cycle
            # resets the stop posture before the escalation callback runs.
            slot._stop_state = "idle"
            await on_hard()
            return "hard"

        state.sessions.stop_turn = AsyncMock(side_effect=_stop_turn)

        app = web.Application()
        app["state"] = state

        request = MagicMock()
        # A bare MagicMock answers .get("app") with a truthy mock, which the
        # App Kit 5.2 ownership guard would read as an app token. These cases
        # are dashboard-user presses, so model the absent header explicitly.
        request.get = lambda key, default="": default
        request.app = app
        request.match_info = {"slot": "test-slot"}
        request.query = {}

        with patch("kiro_crew.dashboard.chat_handlers.sel") as mock_sel:
            mock_sel.return_value.log_tool_invocation = MagicMock()
            mock_sel.return_value.log = MagicMock()
            with patch("kiro_crew.dashboard.chat_handlers._reject_pending_approvals"):
                await api_chat_slot_stop(request)

        stop_id = None
        for msg in slot.messages:
            cls_data = json.loads(msg["cls"])
            if cls_data.get("kind") == "stop_event":
                stop_id = cls_data["id"]
        assert stop_id is not None, "handler did not create a stop card"
        assert _card_state(slot, stop_id) == "stop_failed_reset"
        assert slot._stop_event_id is None

    @pytest.mark.asyncio
    async def test_late_soft_ack_does_not_relabel_an_escalated_card(self):
        """Precedence survives: a hard kill is not relabelled a clean stop."""
        from kiro_crew.dashboard.chat_handlers import _make_stop_resolver

        slot = _FakeSlot()
        state = _FakeState(slot)
        stop_id = _seed_stop_card(slot)
        # What the escalation path in api_chat_slot_stop sets on a second press.
        slot._stop_state = "killing"
        slot._stop_escalated_card_id = stop_id

        await _make_stop_resolver(state, slot, "soft", stop_id)()
        assert _card_state(slot, stop_id) == "stopping"
        assert slot._stop_event_id == stop_id

        await _make_stop_resolver(state, slot, "hard", stop_id)()
        assert _card_state(slot, stop_id) == "stop_failed_reset"

    @pytest.mark.asyncio
    async def test_teardown_does_not_erase_hard_kill_precedence(self):
        """Escalation must outlive the teardown that resets `_stop_state`.

        The double-stop ordering that made a `_stop_state == "killing"` guard
        unsound: the second press escalates, `stop_turn(force=True)` awaits
        `reset()`, the runner reaches `_finish_queue_cycle` and resets the state
        to "idle", and only then does the first press's cooperative ack land.
        A state-based guard sees a neutral state and settles the card as a clean
        stop for a session that was killed. `_stop_escalated_card_id` is not reset by
        teardown, so the soft callback still defers.
        """
        from kiro_crew.dashboard.chat_handlers import _make_stop_resolver

        slot = _FakeSlot()
        state = _FakeState(slot)
        stop_id = _seed_stop_card(slot)
        slot._stop_escalated_card_id = stop_id
        # Teardown already won the race, so the state carries no escalation.
        slot._stop_state = "idle"

        await _make_stop_resolver(state, slot, "soft", stop_id)()
        assert _card_state(slot, stop_id) == "stopping"
        assert slot._stop_event_id == stop_id

        await _make_stop_resolver(state, slot, "hard", stop_id)()
        assert _card_state(slot, stop_id) == "stop_failed_reset"
        assert slot._stop_escalated_card_id is None

    def test_stopping_setter_leaves_the_escalation_marker_alone(self):
        """Pin the non-racy property on the real slot, not the stand-in."""
        from kiro_crew.dashboard.state import _ChatSlot

        slot = _ChatSlot("escalation-slot")
        slot._stop_state = "killing"
        slot._stop_escalated_card_id = "stop-esc"

        slot._stopping = False

        assert slot._stop_state == "idle"
        assert slot._stop_escalated_card_id == "stop-esc"

    @pytest.mark.asyncio
    async def test_resolver_settles_the_card_once(self):
        """The card id, not the state, is the idempotency token."""
        from kiro_crew.dashboard.chat_handlers import _make_stop_resolver

        slot = _FakeSlot()
        state = _FakeState(slot)
        stop_id = _seed_stop_card(slot)
        slot._stop_state = "soft_pending"

        resolve_soft = _make_stop_resolver(state, slot, "soft", stop_id)
        await resolve_soft()
        assert _card_state(slot, stop_id) == "stopped"
        assert slot.source_links_invalidated == 1

        # A second callback for the same card must not re-settle it.
        await _make_stop_resolver(state, slot, "hard", stop_id)()
        assert _card_state(slot, stop_id) == "stopped"
        assert slot.source_links_invalidated == 1

    @pytest.mark.asyncio
    async def test_stale_resolver_does_not_settle_a_newer_card(self):
        """A pending callback must not touch a card a later stop opened.

        `stop_turn` awaits these callbacks, so one can still be in flight when
        teardown clears the posture, a new turn starts, and a second stop sweeps
        the old card and opens a new one. A resolver that read the CURRENT id
        would settle the newer card with the older outcome and clear its stop
        posture, so the newer stop's own callback would later find nothing left
        to settle and its card would be wrong rather than merely stranded.
        """
        from kiro_crew.dashboard.chat_handlers import (
            _make_stop_resolver,
            _resolve_stop_event,
        )

        slot = _FakeSlot()
        state = _FakeState(slot)
        old_id = _seed_stop_card(slot, stop_id="stop-old")
        resolver_for_old = _make_stop_resolver(state, slot, "hard", old_id)

        # A second stop runs the handler's stale-card sweep, then opens its own.
        _resolve_stop_event(slot, "soft")
        new_id = _seed_stop_card(slot, stop_id="stop-new")
        slot._stop_state = "soft_pending"

        # The first stop's callback lands late, after the new card exists.
        await resolver_for_old()

        assert _card_state(slot, new_id) == "stopping"
        assert slot._stop_event_id == new_id
        assert slot._stop_state == "soft_pending"
        # The old card keeps the outcome the sweep gave it.
        assert _card_state(slot, old_id) == "stopped"

    @pytest.mark.asyncio
    async def test_escalation_marker_does_not_leak_onto_a_later_card(self):
        """An escalation must not defer a different card's cooperative ack.

        A bare boolean marker recreates the very bug this class covers, one
        layer over. Escalate card A, let a later stop open card B, and B's soft
        ack would defer to a hard callback that belongs to A and will never fire
        for B, leaving B pulsing at "stopping". Scoping the marker to the card id
        makes the stale marker simply stop matching, so no card-open path has to
        remember to clear it.
        """
        from kiro_crew.dashboard.chat_handlers import (
            _make_stop_resolver,
            _resolve_stop_event,
        )

        slot = _FakeSlot()
        state = _FakeState(slot)

        # Card A is escalated to a hard kill.
        old_id = _seed_stop_card(slot, stop_id="stop-old")
        slot._stop_state = "killing"
        slot._stop_escalated_card_id = old_id

        # A later stop sweeps A and opens card B, which was never escalated.
        _resolve_stop_event(slot, "hard")
        new_id = _seed_stop_card(slot, stop_id="stop-new")
        slot._stop_state = "soft_pending"

        # B's own cooperative ack must settle B, not defer to A's escalation.
        await _make_stop_resolver(state, slot, "soft", new_id)()

        assert _card_state(slot, new_id) == "stopped"
        assert slot._stop_event_id is None
        assert slot._stop_state == "idle"

    @pytest.mark.asyncio
    async def test_cardless_hard_kill_still_releases_the_stop_posture(self):
        """An escalation with no card must not strand `_stop_state`.

        `api_chat_slot_interrupt` claims `_stop_state = "soft_pending"` before
        it awaits the request body, and only opens its card afterwards. A
        concurrent `/stop` during that await escalates against a slot that has
        no card, so the hard callback is bound to `card_id=None`. It still has
        to release the posture: a slot left at "killing" suppresses re-queue
        and rejects every later interrupt.
        """
        from kiro_crew.dashboard.chat_handlers import _make_stop_resolver

        slot = _FakeSlot()
        state = _FakeState(slot)
        slot._stop_state = "killing"
        slot._stop_escalated_card_id = None  # nothing to scope to, no card yet

        await _make_stop_resolver(state, slot, "hard", None)()

        assert slot._stop_state == "idle"
        assert slot._stop_event_id is None

    @pytest.mark.asyncio
    async def test_cardless_soft_ack_does_not_read_as_escalated(self):
        """`None == None` must not count as "this card was escalated".

        The marker holds a real card id, so a None-to-None comparison would
        defer a callback that no hard kill will follow, stranding the posture.
        """
        from kiro_crew.dashboard.chat_handlers import _make_stop_resolver

        slot = _FakeSlot()
        state = _FakeState(slot)
        slot._stop_state = "soft_pending"

        await _make_stop_resolver(state, slot, "soft", None)()

        assert slot._stop_state == "idle"


class TestStopCancelsTheSessionTheTurnRunsOn:
    """Stop must address the session the slot's turns actually run on.

    A slot carrying ``linked_session_key`` runs its turns under THAT key —
    ``chat_runner`` resolves it with ``effective_session_key`` — so cancelling
    ``dashboard:<slot key>`` reaches a session that never existed.
    ``SessionManager.stop_turn`` finds nothing, returns "idle", and the handler
    settles the card as "stopped" while the turn keeps streaming: a Stop that
    reports success and does nothing, once per press.
    """

    @staticmethod
    def _request(state):
        from aiohttp import web

        app = web.Application()
        app["state"] = state
        request = MagicMock()
        # A bare MagicMock answers .get("app") with a truthy mock, which the
        # App Kit 5.2 ownership guard would read as an app token. These cases
        # are dashboard-user presses, so model the absent header explicitly.
        request.get = lambda key, default="": default
        request.app = app
        request.match_info = {"slot": "test-slot"}
        request.query = {}
        return request

    @pytest.mark.asyncio
    async def test_stop_uses_the_linked_session_key(self):
        from kiro_crew.dashboard.chat_handlers import api_chat_slot_stop

        slot = _FakeSlot()
        slot.linked_session_key = "cron:40b4958a"
        state = _FakeState(slot)

        with patch("kiro_crew.dashboard.chat_handlers.sel"), patch(
            "kiro_crew.dashboard.chat_handlers._reject_pending_approvals"
        ):
            await api_chat_slot_stop(self._request(state))

        assert state.sessions.stop_turn.await_args.args[0] == "cron:40b4958a"

    @pytest.mark.asyncio
    async def test_stop_falls_back_to_the_dashboard_key(self):
        """A plain chat tab has no linked key, and must keep its own."""
        from kiro_crew.dashboard.chat_handlers import api_chat_slot_stop

        slot = _FakeSlot()
        state = _FakeState(slot)

        with patch("kiro_crew.dashboard.chat_handlers.sel"), patch(
            "kiro_crew.dashboard.chat_handlers._reject_pending_approvals"
        ):
            await api_chat_slot_stop(self._request(state))

        assert state.sessions.stop_turn.await_args.args[0] == "dashboard:test-slot"

    @pytest.mark.asyncio
    async def test_interrupt_uses_the_linked_session_key(self):
        from kiro_crew.dashboard.chat_handlers import api_chat_slot_interrupt

        slot = _FakeSlot()
        slot.linked_session_key = "slack:1786000000.1"
        # /interrupt is only reachable with something queued to promote.
        slot._queue = [{"id": "q1", "content": "next"}]
        state = _FakeState(slot)

        request = self._request(state)
        request.content_length = 0
        # No body sent: read_bounded_json branches on can_read_body, which a
        # bare MagicMock answers truthy — model the absent body explicitly.
        request.can_read_body = False

        with patch("kiro_crew.dashboard.chat_handlers.sel"), patch(
            "kiro_crew.dashboard.chat_handlers._reject_pending_approvals"
        ):
            await api_chat_slot_interrupt(request)

        assert state.sessions.stop_turn.await_args.args[0] == "slack:1786000000.1"
