"""A failed ``/compact`` must not unlink the channel session it recovers.

Slack, Discord and Telegram all tear the session down when compaction raises,
so the next message cold-starts instead of resuming a wedged conversation.
That teardown is *housekeeping*: the user asked to compress the conversation,
not to unbind it. ``SessionManager.destroy`` deletes the whole session-map
entry, and that entry is where the conversation's channel identity lives — the
Slack thread linkage (plus the reverse thread index that routes a reply back to
its session) and the mirror binding a ``/link`` established.

The repo already states this invariant in three places: ``SessionMap.prune``
refuses to delete an entry carrying a channel binding, ``discard_conversation``
exists precisely so the poisoned-conversation escalation can drop a native
conversation without unlinking it, and ``SessionManager._recycle_held`` was
moved onto ``clear_sid`` for the same reason. These three compaction-failure
paths are the ones that were missed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from conftest import MockSlackClient
from kiro_crew.config import KiroCrewConfig
from kiro_crew.messaging.link import ChannelLink
from kiro_crew.session import SessionManager


@pytest.fixture
def cfg():
    c = KiroCrewConfig()
    c.session.timeout_secs = 2
    return c


def _compact_raising_factory():
    """Provider factory whose ``compact()`` blows up mid-turn."""

    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        m = AsyncMock()
        m.start = AsyncMock()
        m.shutdown = AsyncMock()
        m.is_process_alive = lambda: True
        m.context_usage_pct = lambda: 0.0
        m.has_active_turn = lambda: False
        m.compact = AsyncMock(side_effect=RuntimeError("transport closed"))
        return m

    return factory


async def _live_session(mgr: SessionManager, key: str) -> str:
    """Create a released session for *key* and return the folded map key."""
    await mgr.get_or_create(key)
    mgr.release(key)
    folded = mgr._fold_key(key)
    mgr._session_map.set(folded, "sid-live")
    return folded


class TestCompactFailurePreservesChannelIdentity:
    @pytest.mark.asyncio
    async def test_slack_compact_failure_keeps_the_thread_binding(self, cfg):
        """A Slack thread must still resolve to its session after the teardown.

        ``_resolve_thread_owner`` routes every reply in a linked thread through
        ``get_session_for_thread``. Dropping the entry drops the reverse index
        with it, so the next reply mints a brand-new ``slack:<ts>`` session with
        none of the conversation's context.
        """
        from kiro_crew.slack import handler as h

        mgr = SessionManager(cfg, provider_factory=_compact_raising_factory())
        key = await _live_session(mgr, "dashboard:chat-7")
        mgr.set_slack_link(key, "t1", "C1")
        assert mgr.get_session_for_thread("t1") == key, "precondition: thread is bound"

        await h._handle_compact_command(MockSlackClient(), mgr, "C1", "t1", "m1", key)

        assert mgr.get_session_for_thread("t1") == key, "the thread lost its session"
        assert mgr.get_slack_link(key) == ("t1", "C1")
        # The wedged native conversation is still unresumable, and the session
        # itself is still gone — the teardown must keep doing its job.
        assert not mgr._session_map.get(key)
        assert mgr._session_map.get_discarded_sid(key) == "sid-live"
        assert not mgr.has_session(key)
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_discord_compact_failure_keeps_the_mirror_binding(self, cfg):
        """A Discord conversation must still be mirrored after the teardown."""
        from kiro_crew.discord.transport_dispatch import DiscordDispatcher

        mgr = SessionManager(cfg, provider_factory=_compact_raising_factory())
        dispatcher = DiscordDispatcher(
            sessions=mgr,
            ctx_builder=MagicMock(),
            cfg=cfg,
            allowed_user_ids={"U1"},
        )
        dispatcher.client = AsyncMock()
        key = dispatcher._inbound_session_key("U1", "C1", "")
        await _live_session(mgr, key)
        mgr.set_mirror_link(
            key, ChannelLink(channel_type="discord", channel_id="C1"), accepts_inbound=True
        )

        await dispatcher._handle_compact("U1", "C1", "", None)

        link = mgr.get_mirror_link(key)
        assert link is not None, "the conversation lost its mirror binding"
        assert (link.channel_type, link.channel_id) == ("discord", "C1")
        assert mgr.mirror_accepts_inbound(key) is True
        assert not mgr._session_map.get(key)
        assert mgr._session_map.get_discarded_sid(key) == "sid-live"
        assert not mgr.has_session(key)
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_telegram_compact_failure_keeps_the_mirror_binding(self, cfg):
        """A Telegram conversation must still be mirrored after the teardown."""
        from kiro_crew.telegram.transport_dispatch import TelegramDispatcher

        mgr = SessionManager(cfg, provider_factory=_compact_raising_factory())
        dispatcher = TelegramDispatcher(
            sessions=mgr,
            ctx_builder=MagicMock(),
            cfg=cfg,
            allowed_user_ids={"7"},
        )
        dispatcher.client = AsyncMock()
        route = ("7", "")
        key = dispatcher._session_key(route)
        await _live_session(mgr, key)
        mgr.set_mirror_link(
            key, ChannelLink(channel_type="telegram", channel_id="7"), accepts_inbound=True
        )

        await dispatcher._handle_compact(route, 7)

        link = mgr.get_mirror_link(key)
        assert link is not None, "the conversation lost its mirror binding"
        assert (link.channel_type, link.channel_id) == ("telegram", "7")
        assert mgr.mirror_accepts_inbound(key) is True
        assert not mgr._session_map.get(key)
        assert mgr._session_map.get_discarded_sid(key) == "sid-live"
        assert not mgr.has_session(key)
        await mgr.close_all()
