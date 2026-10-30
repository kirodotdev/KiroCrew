"""A Discord, Teams or Webex drain that finds its session held by another turn.

The drain runs after the channel's own turn releases the session. If something else
took the lease in between -- a dashboard, cron or auto-nudge turn on the same key, or
another channel's turn under a unified ``dm_scope`` -- replaying a queued message only
reaches the busy path, which queues the same text again, and the pump dequeued it
straight back. Every lap flipped the receipt to "Now answering" with no turn started.

Covered per channel: nothing is dequeued, replayed or announced while the session is
held; once the lease is free the message is answered exactly once with no new input;
and the client's close retires the drain, so a retry still waiting when the channel is
turned off answers nothing on it. Telegram's copy of the same guard is covered in
``test_telegram_drain_busy_holder.py``; the retry helper itself is covered there too.
"""

from __future__ import annotations

from typing import Any
from unittest import mock

import pytest
from test_discord import _dispatcher as _dc_dispatcher
from test_teams_midturn import _EMAIL as _TEAMS_EMAIL
from test_teams_midturn import _Client as _TeamsClient
from test_teams_midturn import _dispatcher as _teams_dispatcher
from test_teams_midturn import _inbound as _teams_inbound
from test_teams_midturn import _Provider as _TeamsProvider
from test_teams_midturn import _Sessions as _TeamsSessions
from webex_dispatch_helpers import FakeClient as _WebexClient
from webex_dispatch_helpers import FakeCtx as _WebexCtx
from webex_dispatch_helpers import FakeProvider as _WebexProvider
from webex_dispatch_helpers import FakeSessions as _WebexSessions
from webex_dispatch_helpers import _dispatcher as _webex_dispatcher
from webex_dispatch_helpers import _entry as _webex_entry

from kiro_crew.messaging import queue_drain
from kiro_crew.messaging.queue_drain import reset_drains
from kiro_crew.testing.wait import async_wait_until


@pytest.fixture(autouse=True)
def _clean_registry():
    """A drain registration and a pending retry are process-wide; never inherit one."""
    reset_drains()
    yield
    reset_drains()


@pytest.fixture(autouse=True)
def _fast_poll(_floor_monkeypatch):
    """Look at the lease on every loop turn, so a test waits on events, not seconds."""
    _floor_monkeypatch.setattr(queue_drain, "_HELD_POLL_FIRST_SECS", 0.0)
    _floor_monkeypatch.setattr(queue_drain, "_HELD_POLL_MAX_SECS", 0.0)


async def _retry_settled(channel: str, key: str) -> None:
    await async_wait_until(lambda: (channel, key) not in queue_drain._HELD_RETRIES, timeout=10)


class _DequeuedWhileHeld(AssertionError):
    pass


def _forbid_dequeue_while_held(sessions: Any, key: str) -> None:
    """Fail at the first entry taken off a held session's queue.

    That dequeue is the first lap of the loop. Without this a regressed pump would
    replay into the held session, requeue, and spin until the test timed out.
    """
    real = sessions.dequeue

    def _dequeue(k: str) -> Any:
        if sessions.is_busy(key):
            peeked = real(k)
            if peeked is not None:
                raise _DequeuedWhileHeld(f"dequeued {peeked[1]!r} from a held session")
            return None
        return real(k)

    sessions.dequeue = _dequeue


# -- Discord ---------------------------------------------------------------------


def _discord() -> tuple[Any, ...]:
    from kiro_crew.discord.transport_dispatch import _QueuedOrigin

    d, cli, sess = _dc_dispatcher({"u1"})
    key = d._session_key("u1")
    _forbid_dequeue_while_held(sess, key)
    replayed: list[str] = []

    async def _spy(msg: Any, **kw: Any) -> None:
        replayed.append(msg.text)

    d.handle_message = _spy
    sess._busy = True
    origin = _QueuedOrigin(user_id="u1", channel_id="c1", thread_id="")
    return d, cli, sess, replayed, key, origin


def _discord_announced(cli: Any) -> list[str]:
    return [str(e[1]) for e in cli.edits if "Now answering" in str(e[1])]


class TestDiscord:
    @pytest.mark.asyncio
    async def test_a_held_session_waits_then_answers_once(self) -> None:
        d, cli, sess, replayed, key, origin = _discord()
        assert await d._enqueue_with_receipt(key, "c1", "hello", origin=origin)

        await d._drain_queue(key)  # still held by another turn
        assert replayed == [], "no turn can start in a held session"
        assert _discord_announced(cli) == [], "so nothing may claim one has"
        assert [e[1] for e in sess.queued] == ["hello"], "the message stays queued"
        assert ("discord", key) in queue_drain._HELD_RETRIES

        sess._busy = False  # the holder lets go; nothing else happens
        await async_wait_until(lambda: replayed, timeout=10, describe=lambda: sess.queued)
        await _retry_settled("discord", key)
        assert replayed == ["hello"], "answered exactly once"
        assert len(_discord_announced(cli)) == 1, "with exactly one notice"
        assert sess.queued == []

    @pytest.mark.asyncio
    async def test_the_client_close_retires_the_waiting_drain(self) -> None:
        from kiro_crew.discord.gateway import _client_close_hook

        d, cli, sess, replayed, key, origin = _discord()
        assert await d._enqueue_with_receipt(key, "c1", "hello", origin=origin)
        await d._drain_queue(key)
        _client_close_hook(d, d.deliver_spawn_approval)()  # the operator turns it off
        assert "discord" not in queue_drain._DRAINS

        sess._busy = False
        await _retry_settled("discord", key)
        assert replayed == [], "nothing is answered on a closed channel"
        assert [e[1] for e in sess.queued] == ["hello"]

    def test_an_old_clients_close_keeps_a_restarted_dispatchers_drain(self) -> None:
        from kiro_crew.discord.gateway import _client_close_hook

        old, _c1, _s1 = _dc_dispatcher({"u1"})
        new, _c2, _s2 = _dc_dispatcher({"u1"})  # registers itself, replacing ``old``
        _client_close_hook(old, old.deliver_spawn_approval)()
        assert queue_drain._DRAINS.get("discord") == new._drain_queue


# -- Teams -----------------------------------------------------------------------


def _teams(monkeypatch) -> tuple[Any, ...]:
    from kiro_crew.teams.transport_dispatch import _origin_kwargs

    turns: list[str] = []

    async def _fake_drive(turn, **kw):
        turns.append(turn.user_text)

    async def _permitted(_c):
        return True

    monkeypatch.setattr("kiro_crew.teams.transport_dispatch.drive_turn", _fake_drive)
    monkeypatch.setattr("kiro_crew.teams.transport_dispatch.inbound_permitted", _permitted)
    sessions = _TeamsSessions(_TeamsProvider(), busy=True)
    d = _teams_dispatcher(sessions, _TeamsClient())
    key = d._session_key(_TEAMS_EMAIL)
    sessions.queues[key] = [("1", "hello", dict(_origin_kwargs(_teams_inbound("hello"))))]
    _forbid_dequeue_while_held(sessions, key)
    return d, sessions, turns, key


class TestTeams:
    @pytest.mark.asyncio
    async def test_a_held_session_waits_then_answers_once(self, monkeypatch) -> None:
        d, sessions, turns, key = _teams(monkeypatch)

        await d._drain_queue(key, _teams_inbound("opener"))  # still held
        assert turns == [], "no turn can start in a held session"
        assert d.client.updates == [], "and no receipt is flipped"
        assert [e[1] for e in sessions.queues[key]] == ["hello"], "the message stays queued"
        assert ("teams", key) in queue_drain._HELD_RETRIES

        sessions._busy = False
        await async_wait_until(lambda: turns, timeout=10, describe=lambda: sessions.queues)
        await _retry_settled("teams", key)
        assert turns == ["hello"], "answered exactly once"
        assert not sessions.queues.get(key)

    @pytest.mark.asyncio
    async def test_the_client_close_retires_the_waiting_drain(self, monkeypatch) -> None:
        from kiro_crew.teams.client import TeamsClient
        from kiro_crew.teams.gateway import _client_close_hook

        d, sessions, turns, key = _teams(monkeypatch)
        await d._drain_queue(key, _teams_inbound("opener"))
        client = TeamsClient(app_id="a", app_password="p", tenant_id="t")
        client.on_close = _client_close_hook(d)
        await client.close()  # the operator turns it off
        assert "teams" not in queue_drain._DRAINS

        sessions._busy = False
        await _retry_settled("teams", key)
        assert turns == [], "nothing is answered on a closed channel"
        assert [e[1] for e in sessions.queues[key]] == ["hello"]


# -- Webex -----------------------------------------------------------------------


def _webex() -> tuple[Any, ...]:
    sessions = _WebexSessions(_WebexProvider([]))
    sessions._busy = True
    client = _WebexClient()
    d = _webex_dispatcher(sessions, _WebexCtx(), client)
    sessions.queued = [_webex_entry("1", "hello")]
    _forbid_dequeue_while_held(sessions, "KEY")
    return d, sessions, client, "KEY"


class TestWebex:
    @pytest.mark.asyncio
    async def test_a_held_session_waits_then_answers_once(self) -> None:
        d, sessions, client, key = _webex()
        seen: list[str] = []

        async def _replay(self, inbound, *, interpret_commands=True, drain=True):
            seen.append(inbound.text)

        with mock.patch.object(type(d), "handle_message", _replay):
            await d._drain_queue(key)  # still held
            assert seen == [], "no turn can start in a held session"
            assert client.edits == [], "and no receipt is flipped"
            assert [e[1] for e in sessions.queued] == ["hello"], "the message stays queued"
            assert ("webex", key) in queue_drain._HELD_RETRIES

            sessions._busy = False
            await async_wait_until(lambda: seen, timeout=10, describe=lambda: sessions.queued)
            await _retry_settled("webex", key)
        assert seen == ["hello"], "answered exactly once"
        assert sessions.queued == []

    @pytest.mark.asyncio
    async def test_the_client_close_retires_the_waiting_drain(self) -> None:
        from kiro_crew.webex.client import WebexClient
        from kiro_crew.webex.gateway import _client_close_hook

        d, sessions, _client, key = _webex()
        seen: list[str] = []

        async def _replay(self, inbound, *, interpret_commands=True, drain=True):
            seen.append(inbound.text)

        with mock.patch.object(type(d), "handle_message", _replay):
            await d._drain_queue(key)
            client = WebexClient(token="t")
            client.on_close = _client_close_hook(d)
            await client.close()  # the operator turns it off
            assert "webex" not in queue_drain._DRAINS

            sessions._busy = False
            await _retry_settled("webex", key)
        assert seen == [], "nothing is answered on a closed channel"
        assert [e[1] for e in sessions.queued] == ["hello"]
