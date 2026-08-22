"""An app-scoped slot must not acquire a channel session by naming it.

``get_or_create_slot`` resolves ``linked_session_key`` from the session map for
any slot whose name is shaped like a channel session stem. That inference is
correct for the dashboard: a channel-born tab is *named for the very thread it
lives in*, and resolving the binding centrally is what stops the History resume
path from surfacing an unbound tab that answers from the wrong session.

It is not correct for an app token. ``app=`` is applied in the same call, so an
app that asks for a slot named ``slack_<ts>`` is handed a slot it owns which is
bound to a conversation it has no claim on. Ownership then reads as authority:
the App Kit §5.2 check on every chat route compares ``request_app`` against
``slot._app``, passes, and the turn runs on ``effective_session_key(slot)`` —
the channel's session.

The escalation is at BINDING time, so these tests assert on the binding, not on
any one route. Each route-level test names the route that consumes it.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

SLACK_KEY = "slack:1785370133.085469"
SLACK_STEM = "slack_1785370133.085469"
DISCORD_KEY = "discord:kirocrew:direct:123456"
DISCORD_STEM = "discord_kirocrew_direct_123456"

APP = "issue-radar"


@pytest.fixture
def state():
    """A real ``DashboardState`` with the real ``get_or_create_slot``.

    Built with ``__new__`` and the attributes that method touches, so the code
    under test is production's own — stubbing the factory (as some sibling
    suites do) would test the stub's inference instead of the real one.
    """
    from kiro_crew.dashboard.state import DashboardState

    st = DashboardState.__new__(DashboardState)
    st._slots = {}
    st._slots_under_construction = set()
    st._slot_counter = 0
    st._restricted_keys = set()
    st._ephemeral_keys = set()
    st._slack_to_slot = {}
    st.conversation_log = None
    st.sessions = MagicMock()
    st.sessions.channel_key_for_stem.side_effect = lambda stem: (
        SLACK_KEY if stem == SLACK_STEM else DISCORD_KEY if stem == DISCORD_STEM else ""
    )
    # No genuine Slack mirror link on any session — the hydration below the
    # binding is a separate concern and must not colour these assertions.
    st.sessions.get_slack_link.return_value = (None, None)
    st.push_slots_update = MagicMock()
    st._broadcast_chat_message = MagicMock()
    return st


class TestAnAppCannotBindItselfToAChannelSession:
    """The defect: naming a live thread is enough to be bound to it."""

    def test_app_named_slack_stem_does_not_adopt_the_thread_session(self, state):
        from kiro_crew.dashboard.chat_utils import effective_session_key

        slot = state.get_or_create_slot(SLACK_STEM, app=APP)

        assert slot._app == APP
        assert slot.linked_session_key == "", (
            "an app-scoped slot adopted a channel session it never owned; "
            f"linked to {slot.linked_session_key!r}"
        )
        assert (
            effective_session_key(slot) == f"dashboard:{SLACK_STEM}"
        ), "the app's turns resolve onto the channel's own session"

    def test_the_same_holds_for_every_channel_namespace(self, state):
        """Not a Slack-specific hole — ``is_channel_session_key`` covers nine."""
        slot = state.get_or_create_slot(DISCORD_STEM, app=APP)

        assert slot.linked_session_key == ""

    def test_the_live_colon_spelling_is_refused_too(self, state):
        """``_normalize_slot_key`` folds ``slack:<ts>`` onto the same stem."""
        slot = state.get_or_create_slot(SLACK_KEY, app=APP)

        assert slot.linked_session_key == ""


class TestTheDashboardKeepsItsAutoBinding:
    """The inference is load-bearing for a caller with no app scope."""

    def test_dashboard_named_slack_stem_still_binds(self, state):
        from kiro_crew.dashboard.chat_utils import effective_session_key

        slot = state.get_or_create_slot(SLACK_STEM)

        assert slot.linked_session_key == SLACK_KEY
        assert effective_session_key(slot) == SLACK_KEY

    def test_an_explicit_binding_is_still_honoured_for_an_unscoped_caller(self, state):
        """``channel_slot_reconciler`` passes the key it already resolved.

        It is the only production caller that supplies ``linked_session_key``,
        and it supplies no ``app``. That combination must keep working.
        """
        slot = state.get_or_create_slot(SLACK_STEM, linked_session_key=SLACK_KEY)

        assert slot.linked_session_key == SLACK_KEY

    def test_a_name_the_session_map_does_not_hold_stays_unbound(self, state):
        """Pre-existing behaviour: only a real channel key may become a binding."""
        slot = state.get_or_create_slot("slack_not-a-real-thread")

        assert slot.linked_session_key == ""

    def test_a_plain_app_slot_is_unaffected(self, state):
        from kiro_crew.dashboard.chat_utils import effective_session_key

        slot = state.get_or_create_slot("issue-radar-worker-1", app=APP)

        assert slot._app == APP
        assert slot.linked_session_key == ""
        assert effective_session_key(slot) == "dashboard:issue-radar-worker-1"

    def test_an_existing_bound_slot_is_returned_unchanged_to_an_app(self, state):
        """The guard is on CREATION. Ownership still decides access afterwards.

        A dashboard-created channel tab already in ``_slots`` keeps its binding
        when an app asks for it by name; the App Kit §5.2 check on each route is
        what refuses the app, and it refuses precisely because ``_app`` is empty.
        """
        dashboard_slot = state.get_or_create_slot(SLACK_STEM)
        assert dashboard_slot.linked_session_key == SLACK_KEY

        same = state.get_or_create_slot(SLACK_STEM, app=APP)

        assert same is dashboard_slot
        assert same.linked_session_key == SLACK_KEY
        assert same._app == "", "an app must not take ownership of an existing slot"


class TestTheExplicitArgumentIsNotABypass:
    """Closing the name inference is worthless if the argument reopens it.

    No production caller supplies ``app=`` and ``linked_session_key=``
    together: the six app callers (auto-research, the issue-radar crew runtime,
    spec-builder, chat_fork, session_transfer, and the three chat routes) pass
    only ``app=``, and ``channel_slot_reconciler`` — the sole supplier of an
    explicit binding — passes no app. So the combination is not a contract to
    preserve; it is the same capability by another door.
    """

    def test_an_app_scoped_slot_refuses_an_explicitly_passed_binding(self, state):
        from kiro_crew.dashboard.chat_utils import effective_session_key

        slot = state.get_or_create_slot("app-slot", app=APP, linked_session_key=SLACK_KEY)

        assert slot._app == APP, "the app keeps its slot"
        assert slot.linked_session_key == "", "the binding was accepted from the argument"
        assert effective_session_key(slot) == "dashboard:app-slot"

    def test_binding_a_live_app_owned_slot_is_refused_too(self, state):
        """``api_cron_to_chat`` assigns onto a slot that already exists.

        It looks the slot up by name and writes the field directly, so an app
        that squats the `cron-<id>` name would otherwise be handed the cron's
        session by a dashboard user's click.
        """
        slot = state.get_or_create_slot("cron-nightly", app=APP)

        slot.linked_session_key = "cron:nightly"

        assert slot.linked_session_key == ""

    def test_an_unscoped_slot_can_still_be_bound_after_creation(self, state):
        """The mirror: that same assignment is how a dashboard cron tab works."""
        slot = state.get_or_create_slot("cron-nightly")

        slot.linked_session_key = "cron:nightly"

        assert slot.linked_session_key == "cron:nightly"


# ---------------------------------------------------------------------------
# Upgrading past the vulnerable version has to disarm what it already wrote.
# ---------------------------------------------------------------------------


class TestAnUpgradeDisarmsAPoisonedTranscript:
    """A creation-time check alone would leave the exploit durable.

    The vulnerable build persists BOTH halves — ``_save_slot_to_history``
    writes ``meta["app"]`` from ``slot._app`` and ``meta["linked_session_key"]``
    from the binding — and the restore paths assign them independently. So a
    tab poisoned before the upgrade comes back armed after a restart, on a
    gateway whose creation path is already fixed.
    """

    def test_a_slot_poisoned_by_the_old_build_restores_unbound(self, tmp_path, monkeypatch):
        from chat_test_helpers import _make_state

        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history, restore_open_slots
        from kiro_crew.dashboard.chat_utils import effective_session_key

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        state = _make_state(tmp_path / "sessions")

        # The vulnerable state, as the older build held it in memory: an
        # app-owned slot whose channel-shaped name auto-bound a live thread.
        # Written to the backing field because that build had no setter to go
        # through — reproducing the DATA, not the code path that produced it.
        slot = state.get_or_create_slot(SLACK_STEM, app=APP)
        slot._linked_session_key = SLACK_KEY
        assert slot.linked_session_key == SLACK_KEY, "the poisoned state was not set up"
        slot.append("user", "hello", "msg msg-u")
        _save_slot_to_history(state, slot, force=True)
        state._persist_open_slots()

        # Both halves really are on disk — if they were not, the rest of this
        # test would pass for the wrong reason. Note WHERE: a bound slot writes
        # through its link, so the app's ownership tag lands in the Slack
        # thread's own transcript.
        meta = state.conversation_log.get_metadata(SLACK_KEY)
        assert meta.get("app") == APP
        assert meta.get("linked_session_key") == SLACK_KEY

        # Restart: a fresh state, same home, same transcripts.
        restarted = _make_state(tmp_path / "sessions")
        restarted.sessions.channel_key_for_stem.side_effect = lambda stem: (
            SLACK_KEY if stem == SLACK_STEM else ""
        )
        restore_open_slots(restarted)

        revived = restarted._slots.get(SLACK_STEM)
        assert revived is not None, "the tab did not come back at all"
        assert revived._app == APP, "the app lost its own conversation"
        assert (
            revived.linked_session_key == ""
        ), "an upgraded gateway re-armed a binding written by the vulnerable build"
        assert effective_session_key(revived) == f"dashboard:{SLACK_STEM}"
        assert revived.channel_origin is False, (
            "channel provenance was inferred from the refused binding, which hands "
            "back the channel TRANSCRIPT through slot_history_key"
        )

    def test_an_unscoped_channel_tab_still_restores_bound(self, tmp_path, monkeypatch):
        """The mirror: a genuine channel tab must survive the same restart.

        Its metadata carries `linked_session_key` and no `app`, which is the
        shape the reconciler and every real channel-born tab produce.
        """
        from chat_test_helpers import _make_state

        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history, restore_open_slots

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        state = _make_state(tmp_path / "sessions")

        slot = state.get_or_create_slot(
            SLACK_STEM, linked_session_key=SLACK_KEY, channel_origin=True
        )
        assert slot.linked_session_key == SLACK_KEY
        slot.append("user", "hello", "msg msg-u")
        _save_slot_to_history(state, slot, force=True)
        state._persist_open_slots()

        restarted = _make_state(tmp_path / "sessions")
        restore_open_slots(restarted)

        revived = restarted._slots.get(SLACK_STEM)
        assert revived is not None
        assert revived.linked_session_key == SLACK_KEY


# ---------------------------------------------------------------------------
# The same thing through the route an app actually holds.
# ---------------------------------------------------------------------------


@pytest.fixture
def route_state(tmp_path):
    """A real ``DashboardState`` behind the real ``/api/chat`` handler."""
    from chat_test_helpers import _make_state

    st = _make_state(tmp_path)
    st.sessions.channel_key_for_stem.side_effect = lambda stem: (
        SLACK_KEY if stem == SLACK_STEM else ""
    )
    return st


def _app_client_app(state, app_identity: str) -> web.Application:
    """``/api/chat`` with ``token_auth_middleware`` having authenticated an app.

    The middleware is the only thing that stamps ``request["app"]``, and every
    App Kit §5.2 check on these routes reads it — so an app token is modelled
    exactly the way the gateway presents one.
    """

    @web.middleware
    async def _identity(request: web.Request, handler: Any) -> web.StreamResponse:
        request["app"] = app_identity
        return await handler(request)

    from kiro_crew.dashboard.chat_handlers import api_chat

    app = web.Application(middlewares=[_identity])
    app["state"] = state
    app.router.add_post("/api/chat", api_chat)
    return app


class TestThroughTheRouteAnAppHolds:
    """POST /api/chat, app token, caller-supplied channel-shaped slot name."""

    @pytest.mark.asyncio
    async def test_app_chat_does_not_run_its_turn_on_the_channel_session(self, route_state):
        from kiro_crew.dashboard.chat_utils import effective_session_key

        # Stop at dispatch: the defect is which session the turn is aimed at,
        # and running a real turn would need a provider.
        with patch("kiro_crew.dashboard.chat_handlers.spawn_guarded_turn") as spawn:
            spawn.return_value = MagicMock()
            async with TestClient(TestServer(_app_client_app(route_state, APP))) as client:
                await client.post("/api/chat", json={"slot": SLACK_STEM, "message": "hi"})

        slot = route_state._slots.get(SLACK_STEM)
        assert slot is not None, "the route did not create the slot"
        assert slot._app == APP
        assert (
            effective_session_key(slot) != SLACK_KEY
        ), "an app token's turn is addressed at a Slack thread's own session"
        assert slot.linked_session_key == ""

    @pytest.mark.asyncio
    async def test_a_dashboard_user_on_the_same_route_still_reaches_the_thread(self, route_state):
        """The mirror case — no app token, so the binding must still happen."""
        from kiro_crew.dashboard.chat_handlers import api_chat
        from kiro_crew.dashboard.chat_utils import effective_session_key

        app = web.Application()
        app["state"] = route_state
        app.router.add_post("/api/chat", api_chat)
        with patch("kiro_crew.dashboard.chat_handlers.spawn_guarded_turn") as spawn:
            spawn.return_value = MagicMock()
            async with TestClient(TestServer(app)) as client:
                await client.post("/api/chat", json={"slot": SLACK_STEM, "message": "hi"})

        slot = route_state._slots.get(SLACK_STEM)
        assert slot is not None
        assert effective_session_key(slot) == SLACK_KEY

    @pytest.mark.asyncio
    async def test_continue_has_no_bound_slot_left_to_escalate_through(self, route_state):
        """``/continue`` is closed by the same fix, not by a second guard.

        Its own App Kit §5.2 check compares ``request_app`` against
        ``slot._app`` and passes — the slot genuinely IS the app's. What made
        the continuation dangerous was the binding underneath it, so once the
        slot cannot be bound at creation there is nothing for ``/continue`` to
        dispatch into. Asserted on the key the continuation would address.
        """
        from kiro_crew.dashboard.chat_utils import effective_session_key

        with patch("kiro_crew.dashboard.chat_handlers.spawn_guarded_turn") as spawn:
            spawn.return_value = MagicMock()
            async with TestClient(TestServer(_app_client_app(route_state, APP))) as client:
                await client.post("/api/chat", json={"slot": SLACK_STEM, "message": "hi"})

        slot = route_state._slots[SLACK_STEM]
        # The ownership predicate /continue applies, spelled as the handler
        # spells it — it passes, and that is the point.
        assert not (APP and APP != slot._app), "ownership alone does not refuse this caller"
        assert effective_session_key(slot) == f"dashboard:{SLACK_STEM}"
