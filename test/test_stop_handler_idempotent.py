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
        # Mirrors the real slot's monotonic stop-initiation counter. The real
        # `_stop_state` setter bumps it on every idle -> active edge; this fake
        # has a plain attribute, so tests that model a second press bump it
        # explicitly.
        self._stop_generation = 0
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

    def queue_promote_by_id(self, queue_id):
        for i, item in enumerate(self._queue):
            if item.get("id") == queue_id:
                self._queue.insert(0, self._queue.pop(i))
                return True
        return False

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

    @pytest.mark.asyncio
    async def test_promotion_lands_even_when_the_claim_was_superseded(self):
        """ "Run this next" must not be silently dropped by the stand-down.

        A benign interleaving supersedes the claim without any escalation: the
        running turn ends naturally during the body read, and teardown resets
        the posture to idle (generation unmoved). On main the promotion landed
        on this exact interleaving; the stand-down must not regress it —
        promotion happens BEFORE the supersede check (a no-op against the
        cleared queue in the genuine escalation case).
        """
        from aiohttp import web

        from kiro_crew.dashboard.chat_handlers import api_chat_slot_interrupt

        slot = _FakeSlot()
        slot.running = True
        slot._queue = [{"id": "q1", "content": "first"}, {"id": "q2", "content": "second"}]

        class TurnEndsPayload(BodyStreamPayload):
            """The running turn ends naturally during the body read."""

            async def iter_chunked(self, n: int):
                slot._stop_state = "idle"  # teardown; no generation bump
                async for chunk in super().iter_chunked(n):
                    yield chunk

        state = _FakeState(slot)
        app = web.Application()
        app["state"] = state

        request = MagicMock()
        request.get = lambda key, default="": default
        request.app = app
        request.match_info = {"slot": "test-slot"}
        raw = b'{"queue_id": "q2"}'
        request.content = TurnEndsPayload(raw)
        request.content_length = len(raw)
        request.can_read_body = True
        request.charset = None

        with patch("kiro_crew.dashboard.chat_handlers.sel") as mock_sel:
            mock_sel.return_value.log_tool_invocation = MagicMock()
            mock_sel.return_value.log = MagicMock()
            with patch("kiro_crew.dashboard.chat_handlers._reject_pending_approvals"):
                resp = await api_chat_slot_interrupt(request)
        body = json.loads(resp.body)

        assert body.get("ok") is True
        # The selected message leads the queue despite the stand-down.
        assert [item["id"] for item in slot._queue] == ["q2", "q1"]

    @pytest.mark.asyncio
    async def test_stale_interrupt_does_not_overwrite_a_later_selection(self):
        """Promotion is generation-gated: a superseded claim must not promote.

        Interrupt A stalls in its body read; its turn ends and interrupt B
        claims a NEWER generation and promotes ITS selection. When A resumes,
        promoting A's selection would overwrite B's — so A promotes only when
        the generation still matches its claim. The benign same-generation
        teardown case (previous test) keeps its promotion.
        """
        from aiohttp import web

        from kiro_crew.dashboard.chat_handlers import api_chat_slot_interrupt

        slot = _FakeSlot()
        slot.running = True
        slot._queue = [
            {"id": "q1", "content": "first"},
            {"id": "q2", "content": "second"},
            {"id": "q3", "content": "third"},
        ]

        class LaterInterruptPayload(BodyStreamPayload):
            """Interrupt B lands while A reads its body."""

            async def iter_chunked(self, n: int):
                slot._stop_generation += 1
                slot._stop_state = "soft_pending"  # B's claim
                slot.queue_promote_by_id("q2")  # B's selection
                async for chunk in super().iter_chunked(n):
                    yield chunk

        state = _FakeState(slot)
        app = web.Application()
        app["state"] = state

        request = MagicMock()
        request.get = lambda key, default="": default
        request.app = app
        request.match_info = {"slot": "test-slot"}
        raw = b'{"queue_id": "q3"}'  # A's selection
        request.content = LaterInterruptPayload(raw)
        request.content_length = len(raw)
        request.can_read_body = True
        request.charset = None

        with patch("kiro_crew.dashboard.chat_handlers.sel") as mock_sel:
            mock_sel.return_value.log_tool_invocation = MagicMock()
            mock_sel.return_value.log = MagicMock()
            with patch("kiro_crew.dashboard.chat_handlers._reject_pending_approvals"):
                resp = await api_chat_slot_interrupt(request)
        body = json.loads(resp.body)

        assert body.get("info") == "stop already in progress"
        # B's selection still leads; A's stale promotion never landed.
        assert [item["id"] for item in slot._queue] == ["q2", "q1", "q3"]

    @pytest.mark.asyncio
    async def test_later_stops_claim_is_not_mistaken_for_ours(self):
        """The stand-down compares generation, not just the state value.

        Ordering: /interrupt claims → concurrent /stop escalates, its hard
        resolver settles to idle → a FURTHER press initiates a fresh soft stop
        (new generation, same "soft_pending" value) and opens its own live
        card. The resumed interrupt must not read that later claim as its own
        and re-arm the other stop's live card to "interrupting".
        """
        from aiohttp import web

        from kiro_crew.dashboard.chat_handlers import api_chat_slot_interrupt

        slot = _FakeSlot()
        slot.running = True
        slot._queue = [{"id": "q1", "content": "hello"}]

        class SupersedingPayload(BodyStreamPayload):
            """Simulates escalate → settle → fresh press during the await."""

            async def iter_chunked(self, n: int):
                # The fresh press's claim: same value, NEWER generation, its
                # own live card.
                slot._stop_generation += 1
                slot._stop_state = "soft_pending"
                _seed_stop_card(slot, stop_id="stop-live2")
                async for chunk in super().iter_chunked(n):
                    yield chunk

        state = _FakeState(slot)
        app = web.Application()
        app["state"] = state

        request = MagicMock()
        request.get = lambda key, default="": default
        request.app = app
        request.match_info = {"slot": "test-slot"}
        raw = b"{}"
        request.content = SupersedingPayload(raw)
        request.content_length = len(raw)
        request.can_read_body = True
        request.charset = None

        with patch("kiro_crew.dashboard.chat_handlers.sel") as mock_sel:
            mock_sel.return_value.log_tool_invocation = MagicMock()
            mock_sel.return_value.log = MagicMock()
            with patch("kiro_crew.dashboard.chat_handlers._reject_pending_approvals"):
                resp = await api_chat_slot_interrupt(request)
        body = json.loads(resp.body)

        assert body.get("info") == "stop already in progress"
        # The later stop's live card is untouched.
        assert _card_state(slot, "stop-live2") == "stopping"
        assert slot._stop_event_id == "stop-live2"
        assert slot._stop_state == "soft_pending"

    @pytest.mark.asyncio
    async def test_rollback_does_not_wipe_a_later_presses_live_claim(self):
        """The rollback branches carry the same generation identity.

        The same ABA the stand-down guard closes: our claim is escalated,
        settled, and a THIRD press re-claims "soft_pending" (new generation)
        during our body await — then our body read FAILS. A value-only
        rollback would reset that press's live claim to idle mid-cancel and
        re-enable auto-run while a real stop is in flight.
        """
        from aiohttp import web

        from kiro_crew.dashboard.chat_handlers import api_chat_slot_interrupt

        slot = _FakeSlot()
        slot.running = True
        slot._queue = [{"id": "q1", "content": "hello"}]
        slot._auto_run = True  # restored-on-rollback value

        class SupersedingPayload(BodyStreamPayload):
            """Third press claims during the await; then the body is refused."""

            async def iter_chunked(self, n: int):
                slot._stop_generation += 1
                slot._stop_state = "soft_pending"  # the third press's claim
                slot._auto_run = False  # its own disable
                async for chunk in super().iter_chunked(n):
                    yield chunk

        state = _FakeState(slot)
        app = web.Application()
        app["state"] = state

        request = MagicMock()
        request.get = lambda key, default="": default
        request.app = app
        request.match_info = {"slot": "test-slot"}
        raw = b'["not", "an", "object"]'  # refused by the body guard
        request.content = SupersedingPayload(raw)
        request.content_length = len(raw)
        request.can_read_body = True
        request.charset = None

        resp = await api_chat_slot_interrupt(request)

        assert resp.status == 400
        # The third press's claim survives our rollback.
        assert slot._stop_state == "soft_pending"
        assert slot._auto_run is False

    @pytest.mark.asyncio
    async def test_superseded_claim_does_not_touch_the_live_escalation(self):
        """An interrupt whose claim was escalated mid-await stands down.

        A concurrent /stop during the body await escalates ``soft_pending`` to
        ``killing`` and scopes the LIVE escalation marker to the open card.
        Continuing would re-arm that card and — because the reuse path clears a
        marker matching the reused id — erase the live escalation, so a late
        cooperative ack could relabel the hard kill as a clean stop. The
        escalation owns the stop now; the superseded interrupt answers like
        the idempotent-repeat branch and touches nothing.
        """
        from aiohttp import web

        from kiro_crew.dashboard.chat_handlers import api_chat_slot_interrupt

        slot = _FakeSlot()
        slot.running = True
        slot._queue = [{"id": "q1", "content": "hello"}]
        _seed_stop_card(slot, stop_id="stop-live")

        class EscalatingPayload(BodyStreamPayload):
            """Body stream that simulates a concurrent /stop escalation."""

            async def iter_chunked(self, n: int):
                slot._stop_state = "killing"
                slot._stop_escalated_card_id = "stop-live"
                async for chunk in super().iter_chunked(n):
                    yield chunk

        state = _FakeState(slot)
        app = web.Application()
        app["state"] = state

        request = MagicMock()
        request.get = lambda key, default="": default
        request.app = app
        request.match_info = {"slot": "test-slot"}
        raw = b"{}"
        request.content = EscalatingPayload(raw)
        request.content_length = len(raw)
        request.can_read_body = True
        request.charset = None

        with patch("kiro_crew.dashboard.chat_handlers.sel") as mock_sel:
            mock_sel.return_value.log_tool_invocation = MagicMock()
            mock_sel.return_value.log = MagicMock()
            with patch("kiro_crew.dashboard.chat_handlers._reject_pending_approvals"):
                resp = await api_chat_slot_interrupt(request)
        body = json.loads(resp.body)

        assert body.get("info") == "stop already in progress"
        # The live escalation is untouched: marker intact, card not re-armed,
        # no second card, posture still the hard kill's.
        assert slot._stop_escalated_card_id == "stop-live"
        assert _card_state(slot, "stop-live") == "stopping"
        assert slot._stop_state == "killing"
        assert len([m for m in slot.messages if "stop_event" in (m.get("cls") or "")]) == 1


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
        stops mapping falsy to "idle", those simulations stop being faithful.
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


class TestStopReusesOrphanedCard:
    """One Stop press yields exactly ONE ``stop_event`` row on the wire.

    The old "defensive stale-card sweep" resolved an orphaned card AND appended
    a fresh one, so a single press put TWO rows on the wire — the pane upserts
    by ``meta.id``, and two distinct ids render as two "[Stopped]" chips. The
    fix re-arms the orphaned row in place (same id), mirroring how the
    escalation path reuses the open card via ``_stop_escalated_card_id`` rather
    than minting a second one.
    """

    @staticmethod
    def _stop_event_rows(slot):
        rows = []
        for msg in slot.messages:
            try:
                data = json.loads(msg.get("cls") or "")
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(data, dict) and data.get("kind") == "stop_event":
                rows.append(data)
        return rows

    async def _press(self, state, slot):
        from kiro_crew.dashboard.chat_handlers import stop_slot_turn

        with patch("kiro_crew.dashboard.chat_handlers.sel") as mock_sel:
            mock_sel.return_value.log_tool_invocation = MagicMock()
            mock_sel.return_value.log = MagicMock()
            with patch("kiro_crew.dashboard.chat_handlers._reject_pending_approvals"):
                return await stop_slot_turn(state, slot)

    @pytest.mark.asyncio
    async def test_stale_card_press_yields_one_row_reusing_its_id(self):
        """A press that finds an orphan re-arms it: one row, same id."""
        slot = _FakeSlot()
        state = _FakeState(slot)
        # "soft" so the handler's outcome == "idle" resolve branch stays out of
        # the way: the card must be observable in its re-armed state.
        state.sessions.stop_turn = AsyncMock(return_value="soft")
        _seed_stop_card(slot, stop_id="stop-orphan")
        rebroadcasts = []
        slot._on_message = lambda key, msg: rebroadcasts.append(msg)

        await self._press(state, slot)

        rows = self._stop_event_rows(slot)
        assert len(rows) == 1
        assert rows[0]["id"] == "stop-orphan"
        assert rows[0]["state"] == "stopping"
        assert slot._stop_event_id == "stop-orphan"
        # The re-arm reaches connected panes the same way a resolve does.
        assert len(rebroadcasts) == 1

    @pytest.mark.asyncio
    async def test_reuse_clears_a_stale_escalation_marker(self):
        """A marker scoped to the reused id must not defer the new soft ack.

        With a NEW card the stale marker simply stopped matching (see
        ``test_escalation_marker_does_not_leak_onto_a_later_card``). Reuse makes
        the ids EQUAL, so the re-arm has to clear the marker explicitly or this
        press's cooperative ack defers to a hard callback that already fired,
        stranding the re-armed card at "stopping".
        """
        from kiro_crew.dashboard.chat_handlers import _make_stop_resolver

        slot = _FakeSlot()
        state = _FakeState(slot)
        state.sessions.stop_turn = AsyncMock(return_value="soft")
        _seed_stop_card(slot, stop_id="stop-orphan")
        slot._stop_escalated_card_id = "stop-orphan"  # prior press escalated

        await self._press(state, slot)
        assert slot._stop_escalated_card_id is None

        # This press's own cooperative ack settles the re-armed card.
        await _make_stop_resolver(state, slot, "soft", "stop-orphan")()
        assert _card_state(slot, "stop-orphan") == "stopped"

    @pytest.mark.asyncio
    async def test_orphan_id_with_no_row_still_yields_one_row(self):
        """An orphaned id whose row is gone falls back to a single append.

        With no row in the window there is no chip whose identity needs
        preserving: the stale posture is settled (clearing the id) and this
        press appends its one card under a fresh id.
        """
        slot = _FakeSlot()
        state = _FakeState(slot)
        state.sessions.stop_turn = AsyncMock(return_value="soft")
        slot._stop_event_id = "stop-vanished"  # id without a matching row

        await self._press(state, slot)

        rows = self._stop_event_rows(slot)
        assert len(rows) == 1
        assert rows[0]["id"] == slot._stop_event_id
        assert rows[0]["id"] != "stop-vanished"
        assert rows[0]["state"] == "stopping"

    @pytest.mark.asyncio
    async def test_prior_press_resolver_does_not_settle_the_reused_card(self):
        """A pending resolver from an EARLIER stop must not touch the reuse.

        Card reuse makes the id guard insufficient by construction: the prior
        press's callback is bound to the SAME id the new press re-armed, so
        matching ids cannot prove matching stops. The resolver therefore
        also binds ``slot._stop_generation`` (bumped by the real ``_stop_state``
        setter on every idle -> active edge) and bails when a newer stop has
        initiated since it was created — the newer stop's own callbacks own
        both the card and the posture. (GPT server-lane blocking finding on
        head 82d79041f.)
        """
        from kiro_crew.dashboard.chat_handlers import _make_stop_resolver

        slot = _FakeSlot()
        state = _FakeState(slot)
        state.sessions.stop_turn = AsyncMock(return_value="soft")

        # Press 1: card opened, resolver bound, then the turn tears down
        # leaving the card orphaned (posture idle, id still set).
        _seed_stop_card(slot, stop_id="stop-orphan")
        slot._stop_generation = 1  # the press's own idle -> active bump
        resolver_from_press_1 = _make_stop_resolver(state, slot, "hard", "stop-orphan")
        slot._stop_state = "idle"  # teardown; generation never rewinds

        # Press 2: initiation bumps the generation (real setter behavior),
        # handler re-arms the orphan under the same id.
        slot._stop_generation = 2
        await self._press(state, slot)
        assert _card_state(slot, "stop-orphan") == "stopping"

        # Press 1's callback lands late: same card id, older generation.
        await resolver_from_press_1()
        assert _card_state(slot, "stop-orphan") == "stopping"
        assert slot._stop_event_id == "stop-orphan"
        assert slot._stop_state == "soft_pending"

        # Press 2's own resolver (current generation) still settles normally.
        await _make_stop_resolver(state, slot, "soft", "stop-orphan")()
        assert _card_state(slot, "stop-orphan") == "stopped"
        assert slot._stop_state == "idle"

    @pytest.mark.asyncio
    async def test_cross_turn_orphan_is_settled_and_a_fresh_card_appended(self):
        """An orphan from a PREVIOUS turn is not re-armed in place.

        Re-arming mutates the row where press 1 appended it — above the
        intervening user prompt — so the current turn would show no chip and
        the transition would play out in earlier scrollback, attributing the
        stop to the wrong turn. Reuse is for the SAME-turn orphan (the adjacent-
        chips repro); a cross-turn orphan is settled where it
        lies and this press's card is appended fresh, in this turn.
        """
        slot = _FakeSlot()
        state = _FakeState(slot)
        state.sessions.stop_turn = AsyncMock(return_value="soft")
        _seed_stop_card(slot, stop_id="stop-prev-turn")
        # The next turn began: a user row now sits after the orphan.
        slot.append("user", "next prompt", "")

        await self._press(state, slot)

        rows = self._stop_event_rows(slot)
        assert len(rows) == 2
        states = {r["id"]: r["state"] for r in rows}
        assert states["stop-prev-turn"] == "stopped"
        new_id = slot._stop_event_id
        assert new_id and new_id != "stop-prev-turn"
        assert states[new_id] == "stopping"
        # The fresh card is the LAST row — in the turn the user stopped.
        assert json.loads(slot.messages[-1]["cls"])["id"] == new_id

    @pytest.mark.asyncio
    async def test_nudge_and_subagent_openers_also_bound_the_reuse(self):
        """Every turn-opening role is a boundary, not just ``user``.

        A monitor cycle opens its turn with a ``nudge`` row and a queue-drained
        completion with a ``subagent`` row (TURN_OPENER_ROLES in
        groupDisplayItems.ts). An orphan above one of those sits in the
        previous visual turn exactly like one above a user prompt, so re-arming
        it would put the press's chip in the wrong turn's block.
        """
        for opener in ("nudge", "subagent"):
            slot = _FakeSlot()
            state = _FakeState(slot)
            state.sessions.stop_turn = AsyncMock(return_value="soft")
            _seed_stop_card(slot, stop_id="stop-prev-turn")
            slot.append(opener, "next turn opener", "")

            await self._press(state, slot)

            rows = self._stop_event_rows(slot)
            assert len(rows) == 2, opener
            states = {r["id"]: r["state"] for r in rows}
            assert states["stop-prev-turn"] == "stopped", opener
            assert slot._stop_event_id != "stop-prev-turn", opener

    @pytest.mark.asyncio
    async def test_failed_rearm_mints_a_fresh_id_for_the_append(self):
        """The append fallback must not reuse the stale id.

        A client's message list is trimmed independently of ``slot.messages``,
        so a fresh append carrying the OLD id would upsert into a client still
        holding the old row — landing the chip in old scrollback, the failure
        mode reuse exists to avoid. When the re-arm cannot find the row, the
        append mints fresh.
        """
        from kiro_crew.dashboard import chat_handlers as ch

        slot = _FakeSlot()
        state = _FakeState(slot)
        state.sessions.stop_turn = AsyncMock(return_value="soft")
        _seed_stop_card(slot, stop_id="stop-orphan")  # same-turn orphan
        with patch.object(ch, "_rearm_stop_event", return_value=False):
            await self._press(state, slot)

        rows = self._stop_event_rows(slot)
        appended = [r for r in rows if r["state"] == "stopping" and r["id"] != "stop-orphan"]
        assert len(appended) == 1
        assert slot._stop_event_id == appended[0]["id"]

    @pytest.mark.asyncio
    async def test_synthesis_injection_also_bounds_the_reuse(self):
        """The frontend's second turn-flushing path is a boundary too.

        ``isSynthesisInjection`` (groupDisplayItems.ts) closes the open batch
        for a ``role == "inject"`` row carrying ``meta.injectKind ==
        "synthesis"`` — the row ``_run_pending_synthesis`` appends when a
        sub-agent wave completes. An orphan above one is cross-turn even
        though plain inject rows (cron/recovery notes) are passive.
        """
        slot = _FakeSlot()
        state = _FakeState(slot)
        state.sessions.stop_turn = AsyncMock(return_value="soft")
        _seed_stop_card(slot, stop_id="stop-prev-turn")
        slot.messages.append(
            {
                "role": "inject",
                "content": "synthesis prompt",
                "cls": "msg msg-inject",
                "meta": {"injectKind": "synthesis"},
            }
        )

        await self._press(state, slot)

        rows = self._stop_event_rows(slot)
        assert len(rows) == 2
        states = {r["id"]: r["state"] for r in rows}
        assert states["stop-prev-turn"] == "stopped"
        assert slot._stop_event_id != "stop-prev-turn"

    @pytest.mark.asyncio
    async def test_plain_inject_rows_are_walked_past(self):
        """A passive inject note (no synthesis kind) is NOT a boundary."""
        slot = _FakeSlot()
        state = _FakeState(slot)
        state.sessions.stop_turn = AsyncMock(return_value="soft")
        _seed_stop_card(slot, stop_id="stop-orphan")
        slot.messages.append(
            {"role": "inject", "content": "recovery note", "cls": "msg msg-inject"}
        )

        await self._press(state, slot)

        rows = self._stop_event_rows(slot)
        assert len(rows) == 1
        assert rows[0]["id"] == "stop-orphan"

    @pytest.mark.asyncio
    async def test_fresh_press_appends_exactly_one_card(self):
        """Control: no orphan means one appended card, as before."""
        slot = _FakeSlot()
        state = _FakeState(slot)
        state.sessions.stop_turn = AsyncMock(return_value="soft")

        await self._press(state, slot)

        rows = self._stop_event_rows(slot)
        assert len(rows) == 1
        assert rows[0]["state"] == "stopping"
        assert slot._stop_event_id == rows[0]["id"]


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

        with (
            patch("kiro_crew.dashboard.chat_handlers.sel"),
            patch("kiro_crew.dashboard.chat_handlers._reject_pending_approvals"),
        ):
            await api_chat_slot_stop(self._request(state))

        assert state.sessions.stop_turn.await_args.args[0] == "cron:40b4958a"

    @pytest.mark.asyncio
    async def test_stop_falls_back_to_the_dashboard_key(self):
        """A plain chat tab has no linked key, and must keep its own."""
        from kiro_crew.dashboard.chat_handlers import api_chat_slot_stop

        slot = _FakeSlot()
        state = _FakeState(slot)

        with (
            patch("kiro_crew.dashboard.chat_handlers.sel"),
            patch("kiro_crew.dashboard.chat_handlers._reject_pending_approvals"),
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

        with (
            patch("kiro_crew.dashboard.chat_handlers.sel"),
            patch("kiro_crew.dashboard.chat_handlers._reject_pending_approvals"),
        ):
            await api_chat_slot_interrupt(request)

        assert state.sessions.stop_turn.await_args.args[0] == "slack:1786000000.1"
