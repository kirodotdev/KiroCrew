"""Fresh start on a crewmate's pinned DM: one press, a clean context, same slot.

``POST /api/members/{slug}/fresh-start`` runs three existing mechanisms in a
fixed order and adds none of its own:

1. stop the running turn (``stop_slot_turn``; a second call while the first is
   still pending is that function's own hard-kill escalation, which is what
   frees a stuck process);
2. clear the conversation through the typed ``/clear`` path, so the next
   turn cold-starts with no history and the transcript records the reset;
3. nothing else: the discard tears the agent process down, so the next turn
   starts a fresh one under the SAME slot key.

The slot key never changes: monitor loops, the work ledger and the crew log are
bound to it. The route is the owner's only, gated exactly like the other
member routes.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

import kiro_crew.dashboard.chat_handlers as ch
from kiro_crew import members
from kiro_crew.dashboard.chat_utils import effective_session_key
from kiro_crew.dashboard.handlers import members as m
from kiro_crew.eventlog import types as eventlog_types
from kiro_crew.eventlog.service import get_service, set_service
from kiro_crew.members import DM_SLOT_MODE, member_slot_key, write_dm_binding

SLUG = "code-reviewer"
MEMBER = "code-reviewer"


@pytest.fixture(autouse=True)
def _fresh_eventlog(tmp_path, monkeypatch):
    """Re-root the member event log at tmp_path, same fixture as
    test_members_eventlog_wiring.py: ``get_service()`` is a singleton, so a
    test observing it must drop any instance a prior test built elsewhere.
    """
    monkeypatch.setattr(members, "data_home", lambda: tmp_path)
    set_service(None)
    yield
    set_service(None)


def _app(state) -> web.Application:
    @web.middleware
    async def _auth(request: web.Request, handler):
        request["app"] = request.headers.get("X-Test-App", "")
        request["user"] = request.headers.get("X-Test-User", "local-app")
        return await handler(request)

    app = web.Application(middlewares=[_auth])
    app["state"] = state
    app.router.add_post("/api/members/{slug}/fresh-start", m.api_member_fresh_start)
    return app


def _bound_slot(state, *, running: bool = False):
    key = member_slot_key(SLUG)
    write_dm_binding(SLUG, member=MEMBER, slot_key=key)
    slot = state.get_or_create_slot(key, agent=MEMBER, mode=DM_SLOT_MODE)
    if running:
        slot.task = MagicMock(done=MagicMock(return_value=False))
    return slot


class _Stops:
    """Stand-in for ``stop_slot_turn`` that records each call.

    ``settle_after`` is the call number that ends the turn: 1 for a turn the
    cooperative cancel stops, 2 for one only the escalation stops, 0 for one
    nothing stops.
    """

    def __init__(self, order: list[str], settle_after: int) -> None:
        self.order = order
        self.settle_after = settle_after
        self.calls = 0

    async def __call__(self, state, slot, **kwargs):
        self.calls += 1
        self.order.append(f"stop{self.calls}")
        if self.settle_after and self.calls >= self.settle_after:
            slot.task = None
        return {"ok": True}


@pytest.fixture
def fast_waits():
    with patch.object(m, "_FRESH_START_STOP_WAIT_SECS", 0.05):
        yield


async def _press(state, **headers):
    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.post(f"/api/members/{SLUG}/fresh-start", headers=headers)
        return resp.status, await resp.json()


def _record_discard(state, order: list[str]) -> None:
    async def _discard(*_a, **_kw):
        order.append("discard")
        return True

    state.sessions.discard_conversation = AsyncMock(side_effect=_discard)


class TestOrder:
    @pytest.mark.asyncio
    async def test_idle_thread_resets_without_a_stop(self, tmp_path, fast_waits):
        state = _make_state(tmp_path)
        slot = _bound_slot(state)
        order: list[str] = []
        _record_discard(state, order)
        stops = _Stops(order, settle_after=1)
        with patch.object(ch, "stop_slot_turn", stops):
            status, body = await _press(state)
        assert status == 200
        assert order == ["discard"]
        state.sessions.discard_conversation.assert_awaited_once_with(
            effective_session_key(slot), replay=False, skip_if_busy=True
        )
        assert body["slot"] == slot.key
        assert body["reset_at"]
        assert body["outcome"] == "cleared"
        # The typed /clear path's own line, so the transcript records the reset.
        assert "Conversation cleared" in slot.messages[-1]["content"]

    @pytest.mark.asyncio
    async def test_a_discard_the_session_refuses_stays_queued(self, tmp_path, fast_waits):
        """As with /clear: the discard lands at the next turn boundary."""
        state = _make_state(tmp_path)
        slot = _bound_slot(state)
        state.sessions.discard_conversation = AsyncMock(return_value=False)
        status, body = await _press(state)
        assert status == 200
        assert body["outcome"] == "queued"
        assert slot._pending_discard_conversation_key

    @pytest.mark.asyncio
    async def test_running_turn_is_stopped_before_the_reset(self, tmp_path, fast_waits):
        state = _make_state(tmp_path)
        _bound_slot(state, running=True)
        order: list[str] = []
        _record_discard(state, order)
        with patch.object(ch, "stop_slot_turn", _Stops(order, settle_after=1)):
            status, _ = await _press(state)
        assert status == 200
        assert order == ["stop1", "discard"]

    @pytest.mark.asyncio
    async def test_stuck_turn_takes_the_escalation_then_resets(self, tmp_path, fast_waits):
        state = _make_state(tmp_path)
        _bound_slot(state, running=True)
        order: list[str] = []
        _record_discard(state, order)
        with patch.object(ch, "stop_slot_turn", _Stops(order, settle_after=2)):
            status, _ = await _press(state)
        assert status == 200
        assert order == ["stop1", "stop2", "discard"]

    @pytest.mark.asyncio
    async def test_a_turn_nothing_stops_is_refused_and_nothing_is_discarded(
        self, tmp_path, fast_waits
    ):
        state = _make_state(tmp_path)
        _bound_slot(state, running=True)
        order: list[str] = []
        _record_discard(state, order)
        with patch.object(ch, "stop_slot_turn", _Stops(order, settle_after=0)):
            status, body = await _press(state)
        assert status == 409
        assert body["code"] == "turn_in_flight"
        assert order == ["stop1", "stop2"]

    @pytest.mark.asyncio
    async def test_queued_messages_refuse_before_anything_is_stopped(self, tmp_path, fast_waits):
        """A soft stop keeps the queue, which would then run on the old context."""
        state = _make_state(tmp_path)
        slot = _bound_slot(state, running=True)
        slot._queue.append({"content": "next question"})
        order: list[str] = []
        _record_discard(state, order)
        with patch.object(ch, "stop_slot_turn", _Stops(order, settle_after=1)):
            status, body = await _press(state)
        assert status == 409
        assert body["code"] == "slot_queue_pending"
        assert order == []

    @pytest.mark.asyncio
    async def test_pending_steers_refuse_before_anything_is_stopped(self, tmp_path, fast_waits):
        state = _make_state(tmp_path)
        slot = _bound_slot(state, running=True)
        slot._pending_steers.append("steer this")
        order: list[str] = []
        _record_discard(state, order)
        with patch.object(ch, "stop_slot_turn", _Stops(order, settle_after=1)):
            status, body = await _press(state)
        assert status == 409
        assert body["code"] == "slot_queue_pending"
        assert order == []

    @pytest.mark.asyncio
    async def test_a_message_queued_during_the_stop_refuses_before_the_reset(
        self, tmp_path, fast_waits
    ):
        state = _make_state(tmp_path)
        _bound_slot(state, running=True)
        order: list[str] = []
        _record_discard(state, order)
        stops = _Stops(order, settle_after=1)

        async def _stop_then_queue(st, sl, **kwargs):
            result = await stops(st, sl, **kwargs)
            sl._queue.append({"content": "typed while stopping"})
            return result

        with patch.object(ch, "stop_slot_turn", _stop_then_queue):
            status, body = await _press(state)
        assert status == 409
        assert body["code"] == "slot_queue_pending"
        assert order == ["stop1"]

    @pytest.mark.asyncio
    async def test_the_slot_key_survives(self, tmp_path, fast_waits):
        """No new session: the same slot object stays bound under the same key."""
        state = _make_state(tmp_path)
        slot = _bound_slot(state)
        before = dict(state._slots)
        status, _ = await _press(state)
        assert status == 200
        assert state._slots == before
        assert state._slots[member_slot_key(SLUG)] is slot


class TestResetPersists:
    """A cleared Fresh start folds ``reset_at`` into the member's event log, so
    the Earlier-conversation boundary survives a reload instead of living only
    in the page's React state (#16339, on top of #18797)."""

    @pytest.mark.asyncio
    async def test_a_cleared_reset_is_folded_into_the_roster_projection(self, tmp_path, fast_waits):
        state = _make_state(tmp_path)
        _bound_slot(state)
        order: list[str] = []
        _record_discard(state, order)
        with patch.object(ch, "stop_slot_turn", _Stops(order, settle_after=1)):
            status, body = await _press(state)
        assert status == 200
        assert body["outcome"] == "cleared"

        svc = get_service()
        slug = members.slug_for_name(MEMBER)
        roster = svc.snapshot(slug)["values"].get(eventlog_types.PROJ_ROSTER, {})
        assert roster.get("reset_at") == body["reset_at"]

    @pytest.mark.asyncio
    async def test_a_queued_reset_folds_nothing(self, tmp_path, fast_waits):
        """Only a reset that actually cleared the conversation is folded: a
        queued one has not happened yet, and folding it would fold the
        thread before the clear it describes actually lands."""
        state = _make_state(tmp_path)
        _bound_slot(state)
        state_sessions_discard = state.sessions.discard_conversation
        state_sessions_discard.return_value = False
        status, body = await _press(state)
        assert status == 200
        assert body["outcome"] == "queued"

        svc = get_service()
        slug = members.slug_for_name(MEMBER)
        roster = svc.snapshot(slug)["values"].get(eventlog_types.PROJ_ROSTER, {})
        assert "reset_at" not in roster

    @pytest.mark.asyncio
    async def test_get_api_members_serves_the_folded_reset_at(
        self, tmp_path, monkeypatch, fast_waits
    ):
        """The read side of the round trip: GET /api/members exposes the same
        reset_at the fresh-start route folded, which is what MembersPage reads
        back into ChatPane's foldBefore on a reload."""
        from types import SimpleNamespace

        from kiro_crew.config.loader import KiroCrewAgentConfig

        state = _make_state(tmp_path)
        _bound_slot(state)
        order: list[str] = []
        _record_discard(state, order)
        with patch.object(ch, "stop_slot_turn", _Stops(order, settle_after=1)):
            status, body = await _press(state)
        assert status == 200
        assert body["outcome"] == "cleared"

        @web.middleware
        async def _auth(request: web.Request, handler):
            request["app"] = ""
            request["user"] = "local-app"
            return await handler(request)

        app = web.Application(middlewares=[_auth])
        app["state"] = state
        app.router.add_get("/api/members", m.api_members)
        cfg = SimpleNamespace(
            agents={MEMBER: KiroCrewAgentConfig(kiro_agent=MEMBER)},
            default_agent=MEMBER,
            memory_stores={},
            degraded_sections=frozenset(),
        )
        cfg._content_digest = "fresh-start-test-digest"
        monkeypatch.setattr(m.KiroCrewConfig, "load", staticmethod(lambda: cfg))
        async with TestClient(TestServer(app)) as client:
            data = await (await client.get("/api/members")).json()
        row = next(r for r in data["members"] if r["slug"] == SLUG)
        assert row["reset_at"] == body["reset_at"]


class TestWho:
    @pytest.mark.asyncio
    async def test_non_owner_dashboard_session_is_refused(self, tmp_path, fast_waits):
        state = _make_state(tmp_path)
        state.owner_id = "U_OWNER"
        _bound_slot(state, running=True)
        order: list[str] = []
        _record_discard(state, order)
        with patch.object(ch, "stop_slot_turn", _Stops(order, settle_after=1)):
            status, body = await _press(state, **{"X-Test-User": "U_SOMEONE_ELSE"})
        assert status == 403
        assert body["code"] == "owner_only"
        assert order == []

    @pytest.mark.asyncio
    async def test_app_token_is_refused_as_not_found(self, tmp_path, fast_waits):
        state = _make_state(tmp_path)
        _bound_slot(state)
        order: list[str] = []
        _record_discard(state, order)
        status, _ = await _press(state, **{"X-Test-App": "some-app"})
        assert status == 404
        assert order == []

    @pytest.mark.asyncio
    async def test_unbound_slug_is_not_found(self, tmp_path, fast_waits):
        state = _make_state(tmp_path)
        order: list[str] = []
        _record_discard(state, order)
        status, _ = await _press(state)
        assert status == 404
        assert order == []

    @pytest.mark.asyncio
    async def test_a_slot_that_is_not_the_members_dm_is_not_found(self, tmp_path, fast_waits):
        state = _make_state(tmp_path)
        key = member_slot_key(SLUG)
        write_dm_binding(SLUG, member=MEMBER, slot_key=key)
        state.get_or_create_slot(key, agent="someone-else", mode=DM_SLOT_MODE)
        order: list[str] = []
        _record_discard(state, order)
        status, _ = await _press(state)
        assert status == 404
        assert order == []
