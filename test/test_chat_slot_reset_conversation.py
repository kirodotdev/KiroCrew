"""Resetting a slot's conversation without destroying its record.

Resume is key-driven -- ``resume_sid = self._session_map.get(key)`` -- and a slot
key is stable by design, so reopening a slot continues the conversation it had.
That is correct for a tab the user closed and came back to, and wrong once a
long-lived conversation has drifted, filled up, or outlived the thing it was
about. Until this route existed the only way to break the link was to DELETE the
session from history, which destroys the record in order to reset the pointer.

Three things decide whether the route is correct, and each has a way of failing
silently rather than loudly:

**Which key it clears.** A slot's session is addressed by
``effective_session_key``, which prefers a channel-born slot's
``linked_session_key``. Deriving ``dashboard:<slot>`` unconditionally instead
yields a key no session ever had, so the call succeeds, reports a reset, and
clears nothing.

**Which verb it uses.** ``discard_conversation`` drops the sid and keeps the
entry, because that entry also carries the channel linkage and the reverse index
built from it. ``destroy`` would take the row with it and silently unlink a
mirrored session.

**Who may ask.** An app may reset only slots it created -- the same boundary
``api_chat_slot_delete`` draws, and for the same reason: a slot's conversation is
its own state.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.chat_handlers import api_chat_slot_reset_conversation
from kiro_crew.dashboard.state import DashboardState, _ChatSlot

OWNER = "acme-app"
OTHER = "other-app"


def _make_app(state: DashboardState, *, declared_app: str = "") -> web.Application:
    app = web.Application()
    app["state"] = state

    @web.middleware
    async def _publish_app(request: web.Request, handler):
        # Stands in for the token middleware, which publishes the validated app
        # token's name. Empty for a dashboard user.
        request["app"] = declared_app
        return await handler(request)

    app.middlewares.append(_publish_app)
    app.router.add_post(
        "/api/chat/slots/{slot}/reset-conversation", api_chat_slot_reset_conversation
    )
    return app


def _state(
    *slots: _ChatSlot, subagents: list | None = None, active_turn: bool | None = None
) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state._slots = {s.key: s for s in slots}
    state.sessions = MagicMock()
    state.sessions.discard_conversation = AsyncMock()
    state.sessions.destroy = AsyncMock()
    if active_turn is None:
        # No live provider for this key, which is what a slot restored from history
        # (or never started) looks like: the turn probe has nothing to ask.
        state.sessions.get_provider = MagicMock(return_value=None)
    else:
        provider = MagicMock()
        provider.has_active_turn = MagicMock(return_value=active_turn)
        state.sessions.get_provider = MagicMock(return_value=provider)
    if subagents is None:
        # No registry at all: the sub-agent gate has nothing to probe and abstains,
        # which is what ``MagicMock(spec=DashboardState)`` produces on its own since
        # ``subagents`` is assigned in ``__init__``.
        state.subagents = None
    else:
        subs = MagicMock()
        subs.running_agents_for = MagicMock(return_value=subagents)
        subs._queued_depth = MagicMock(return_value=0)
        state.subagents = subs
    return state


def _slot(key: str, *, app: str = "", running: bool = False, linked: str = "") -> _ChatSlot:
    slot = _ChatSlot(key)
    slot._app = app
    if linked:
        slot.linked_session_key = linked
    if running:
        # ``running`` is derived from the live task, which is what the route reads.
        slot.task = MagicMock(done=MagicMock(return_value=False))
    return slot


async def _post(app: web.Application, slot: str):
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(f"/api/chat/slots/{slot}/reset-conversation")
        body = await resp.json()
        return resp.status, body


class TestTheReplayParameter:
    """``replay`` decides whether the next cold start re-injects the old
    conversation as a ``[CONVERSATION HISTORY]`` block. Default True keeps the
    route's existing behaviour and its own copy true."""

    @pytest.mark.asyncio
    async def test_replay_false_threads_through(self):
        state = _state(_slot("chat-1-foo"))

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/chat-1-foo/reset-conversation",
                json={"replay": False},
            )
            body = await resp.json()

        assert resp.status == 200
        assert body["replay"] is False
        state.sessions.discard_conversation.assert_awaited_once_with(
            "dashboard:chat-1-foo", replay=False, skip_if_busy=True
        )

    @pytest.mark.asyncio
    async def test_a_body_less_post_still_works(self):
        """The route took no body before this parameter existed."""
        state = _state(_slot("chat-1-foo"))

        status, body = await _post(_make_app(state), "chat-1-foo")

        assert status == 200
        assert body["replay"] is True

    def test_the_body_is_read_before_the_busy_guards(self):
        """Source-order guard: reading the body must not widen the teardown race.

        ``await read_bounded_json(...)`` is a suspension whose duration the CLIENT
        controls. Every busy guard below it protects work that can START during a
        suspension — a turn admitted after ``has_active_turn()`` answered False is
        then torn down mid-write by the discard. Parsing the body after the guards
        would stretch that window from one event-loop hop to however long a slow
        body takes to arrive, so the guards must be the LAST thing before the
        teardown.

        Asserted on the source rather than on behaviour because the failure is an
        interleaving: a test that posts a slow body and races a concurrent turn
        would be exactly the timing-dependent flake the testing conventions
        forbid, and it would pass on a fast machine with the bug present.
        """
        import inspect

        from kiro_crew.dashboard import chat_handlers

        src = inspect.getsource(chat_handlers.api_chat_slot_reset_conversation)
        body_read = src.index("await read_bounded_json(")
        first_guard = src.index("state.sessions.get_provider(key)")
        discard = src.index("discard_conversation(key, replay=")

        assert body_read < first_guard, (
            "the body is parsed after the first busy guard, so a slow body widens "
            "the window in which a concurrent turn can start and be torn down"
        )
        assert first_guard < discard, "the guards must sit between the body read and the teardown"


class TestItClearsTheRightThing:
    @pytest.mark.asyncio
    async def test_it_clears_the_slot_s_own_session(self):
        state = _state(_slot("chat-1-foo"))

        status, body = await _post(_make_app(state), "chat-1-foo")

        assert status == 200
        assert body["reset"] is True
        state.sessions.discard_conversation.assert_awaited_once_with(
            "dashboard:chat-1-foo", replay=True, skip_if_busy=True
        )

    @pytest.mark.asyncio
    async def test_a_channel_born_slot_clears_its_channel_session(self):
        """The failure this pins is silent.

        A channel-born slot's turns run on the channel's own session, so its key
        is the channel key. Deriving ``dashboard:<slot>`` instead produces
        ``dashboard:slack:<ts>``, which no session ever had -- the clear finds
        nothing, and the route still answers 200 with ``reset: true``.
        """
        state = _state(_slot("slack_123.456", linked="slack:123.456"))

        status, _ = await _post(_make_app(state), "slack_123.456")

        assert status == 200
        state.sessions.discard_conversation.assert_awaited_once_with(
            "slack:123.456", replay=True, skip_if_busy=True
        )

    @pytest.mark.asyncio
    async def test_it_keeps_the_entry_rather_than_deleting_it(self):
        """``discard_conversation``, never ``destroy``.

        The entry carries the Slack thread/channel linkage and the reverse index
        built from it, so deleting the row to clear a pointer would silently
        unlink a mirrored session -- a reset that costs the caller something it
        never asked to give up.
        """
        state = _state(_slot("chat-1-foo"))

        await _post(_make_app(state), "chat-1-foo")

        state.sessions.destroy.assert_not_awaited()


class TestItRefusesWhenItCannotBeSafe:
    @pytest.mark.asyncio
    async def test_an_unknown_slot_is_not_found(self):
        state = _state(_slot("chat-1-foo"))

        status, body = await _post(_make_app(state), "chat-9-nope")

        assert status == 404
        assert body["code"] == "slot_not_found"
        state.sessions.discard_conversation.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_running_turn_blocks_the_reset(self):
        """Discarding the session a turn is writing into loses that turn's work,
        and the caller cannot tell that from a reset that did nothing."""
        state = _state(_slot("chat-1-foo", running=True))

        status, body = await _post(_make_app(state), "chat-1-foo")

        assert status == 409
        assert body["code"] == "turn_in_flight"
        state.sessions.discard_conversation.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_turn_in_flight_on_the_session_blocks_the_reset(self):
        """The one ``slot.running`` cannot see.

        An inbound channel message runs a turn on the linked SESSION with no
        dashboard task behind it, so the slot's own ``running`` flag stays False.
        Tearing the provider down under it loses that turn's output.
        """
        state = _state(_slot("slack_123.456", linked="slack:123.456"), active_turn=True)

        status, body = await _post(_make_app(state), "slack_123.456")

        assert status == 409
        assert body["code"] == "turn_in_flight"
        state.sessions.discard_conversation.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_settled_session_does_not_block(self):
        """The negative half: having a provider is not being busy."""
        state = _state(_slot("chat-1-foo"), active_turn=False)

        status, _ = await _post(_make_app(state), "chat-1-foo")

        assert status == 200
        state.sessions.discard_conversation.assert_awaited_once_with(
            "dashboard:chat-1-foo", replay=True, skip_if_busy=True
        )

    @pytest.mark.asyncio
    async def test_a_plan_between_stages_blocks_the_reset(self):
        """``running`` reads False BETWEEN an autopilot plan's stages while the
        plan is still mid-flight, so it alone would discard the conversation the
        plan is writing into and cold-start its next stage."""
        slot = _slot("chat-1-foo")
        slot._in_stage_execution = True
        state = _state(slot)

        status, body = await _post(_make_app(state), "chat-1-foo")

        assert status == 409
        assert body["code"] == "slot_orchestrating"
        state.sessions.discard_conversation.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_attached_sub_agents_block_the_reset(self):
        """``discard_conversation`` is a full teardown — it releases the shared
        runtime the parent's children run on. ``running`` is False while they keep
        going, because the parent's turn ends first, so only the sub-agent gate
        catches this."""
        state = _state(_slot("chat-1-foo"), subagents=[{"id": "sub-1"}])

        status, body = await _post(_make_app(state), "chat-1-foo")

        assert status == 409
        assert body["code"] == "slot_subagents_running"
        state.sessions.discard_conversation.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_children_does_not_block(self):
        """The negative half: the gate must not refuse every slot that HAS a
        sub-agent registry, only one with children attached."""
        state = _state(_slot("chat-1-foo"), subagents=[])

        status, _ = await _post(_make_app(state), "chat-1-foo")

        assert status == 200
        state.sessions.discard_conversation.assert_awaited_once_with(
            "dashboard:chat-1-foo", replay=True, skip_if_busy=True
        )

    @pytest.mark.asyncio
    async def test_a_semaphore_held_turn_with_no_prompt_in_flight_is_refused(self):
        """The edge every fast path above misses, closed by the atomic guard.

        The channel-message shape: an inbound message on a linked session has
        acquired the per-session semaphore but not yet put a prompt in flight,
        so ``has_active_turn()`` answers False and ``slot.running`` is False by
        construction (no dashboard task exists). Only ``discard_conversation``'s
        ``skip_if_busy`` — the semaphore probed atomically with the session
        pop — sees it, and its False return must surface as the same
        ``turn_in_flight`` 409 the fast paths give, not as a success.
        """
        state = _state(_slot("slack_123.456", linked="slack:123.456"), active_turn=False)
        # Behave like the real primitive against a busy session: refuse when
        # asked to skip, tear down (losing the leased turn's work) when not.
        # A mutation dropping ``skip_if_busy=True`` then fails on the 409
        # itself, not merely on the call signature.
        state.sessions.discard_conversation = AsyncMock(
            side_effect=lambda key, *, replay=True, skip_if_busy=False: not skip_if_busy
        )
        sel_events: list[dict] = []

        with patch(
            "kiro_crew.dashboard.chat_handlers.sel",
            lambda: SimpleNamespace(log_api_access=lambda **kw: sel_events.append(kw)),
        ):
            status, body = await _post(_make_app(state), "slack_123.456")

        assert status == 409
        assert body["code"] == "turn_in_flight"
        state.sessions.discard_conversation.assert_awaited_once_with(
            "slack:123.456", replay=True, skip_if_busy=True
        )
        # The refusal is a decline, not a completed teardown: logging
        # ``completed`` here would record a reset that never happened.
        assert [e["outcome"] for e in sel_events] == ["denied"]

    @pytest.mark.asyncio
    async def test_an_idle_session_still_tears_down(self):
        """The negative half of the atomic guard: an idle session (semaphore
        free) is discarded and the route reports the reset it performed."""
        state = _state(_slot("chat-1-foo"), active_turn=False)
        state.sessions.discard_conversation.return_value = True
        sel_events: list[dict] = []

        with patch(
            "kiro_crew.dashboard.chat_handlers.sel",
            lambda: SimpleNamespace(log_api_access=lambda **kw: sel_events.append(kw)),
        ):
            status, body = await _post(_make_app(state), "chat-1-foo")

        assert status == 200
        assert body["reset"] is True
        state.sessions.discard_conversation.assert_awaited_once_with(
            "dashboard:chat-1-foo", replay=True, skip_if_busy=True
        )
        assert [e["outcome"] for e in sel_events] == ["completed"]


class TestAppScope:
    @pytest.mark.asyncio
    async def test_an_app_may_reset_a_slot_it_created(self):
        state = _state(_slot("acme-obj-1", app=OWNER))

        status, _ = await _post(_make_app(state, declared_app=OWNER), "acme-obj-1")

        assert status == 200
        state.sessions.discard_conversation.assert_awaited_once_with(
            "dashboard:acme-obj-1", replay=True, skip_if_busy=True
        )

    @pytest.mark.asyncio
    async def test_an_app_cannot_reset_another_app_s_slot(self):
        state = _state(_slot("acme-obj-1", app=OWNER))

        status, body = await _post(_make_app(state, declared_app=OTHER), "acme-obj-1")

        # 404, not 403: a foreign slot must be indistinguishable from a missing
        # one, or the error itself enumerates other apps' slots (CWE-204).
        assert status == 404
        assert body["code"] == "slot_not_found"
        state.sessions.discard_conversation.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_app_cannot_reset_an_unscoped_slot(self):
        """A user's own tab is not an app's to reset."""
        state = _state(_slot("chat-1-mine"))

        status, body = await _post(_make_app(state, declared_app=OWNER), "chat-1-mine")

        assert status == 404
        assert body["code"] == "slot_not_found"
        state.sessions.discard_conversation.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_app_cannot_reset_a_channel_session_its_slot_is_bound_to(self):
        """Owning the slot is not owning the session.

        ``get_or_create_slot`` resolves ``linked_session_key`` from the session map
        for a name shaped like a channel stem, so an app that names a live channel
        thread ends up OWNING a slot bound to a conversation it has no claim on.
        Authorizing on slot ownership alone would turn that binding into capability
        escalation — the app could wipe a channel conversation's resume pointer.
        """
        state = _state(_slot("slack_123.456", app=OWNER, linked="slack:123.456"))

        status, body = await _post(_make_app(state, declared_app=OWNER), "slack_123.456")

        assert status == 404
        assert body["code"] == "slot_not_found"
        state.sessions.discard_conversation.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_dashboard_user_may_reset_an_app_owned_slot(self):
        """The ownership check scopes APPS, not the person whose machine this is."""
        state = _state(_slot("acme-obj-1", app=OWNER))

        status, _ = await _post(_make_app(state, declared_app=""), "acme-obj-1")

        assert status == 200
        state.sessions.discard_conversation.assert_awaited_once_with(
            "dashboard:acme-obj-1", replay=True, skip_if_busy=True
        )
