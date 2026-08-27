"""A transport client's ``close()`` must always close its aiohttp session.

Four of the long-lived messaging clients (Discord, Telegram, WeCom, Webex) own
one ``aiohttp.ClientSession`` and one background task, and each ``close()``
cancels the task, awaits it, and then closes the session. Teams has no polling
task -- it is webhook-driven -- but it owns TWO sessions (a Connector session
and a separately-resolved attachment-download session) behind the same handler
drain, so both are exposed to the same skip.

``task.cancel()`` on a task that has ALREADY finished with an exception is a
no-op, and the following ``await self._task`` re-raises that exception --
``except asyncio.CancelledError`` does not catch it. The reconnect/polling loops
do not catch every exception type, so this is reachable: one unexpected error in
the loop, then a shutdown, and the session close was skipped. A ``CancelledError``
arriving while ``close()`` itself is being awaited did the same, since it is a
``BaseException``.

The cost is a leaked connector with its open sockets for the process lifetime
(aiohttp surfaces it as "Unclosed client session" at GC) and a ``self._session``
left non-None, so nothing retries.

These tests drive each real ``close()`` with a task that died with an error, and
assert the session was closed anyway and the exception still propagated. The
invariant they pin is per-session, not per-client: EVERY session a client owns
must receive a close attempt on shutdown, even when the handler drain above it
is cancelled and even when closing an earlier sibling session raises.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest


class _FakeSession:
    """Just enough ClientSession for close(): a `closed` flag and `close()`."""

    def __init__(self) -> None:
        self.closed = False
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1
        self.closed = True


async def _task_that_died(exc: BaseException) -> asyncio.Task:
    """A task already finished with *exc* — cancel() on it is a no-op."""

    async def _boom() -> None:
        raise exc

    task = asyncio.ensure_future(_boom())
    with pytest.raises(type(exc)):
        await task
    return task


def _discord():
    from kiro_crew.discord.client import DiscordClient

    return DiscordClient(token="t", on_message=AsyncMock())


def _telegram():
    from kiro_crew.telegram.client import TelegramClient

    return TelegramClient(token="t", on_message=AsyncMock())


def _wecom():
    from kiro_crew.wecom.client import WeComClient

    return WeComClient(bot_id="b", secret="s", ws_url="wss://fake", on_message=AsyncMock())


def _webex():
    from kiro_crew.webex.client import WebexClient

    return WebexClient(token="t", on_message=AsyncMock())


def _teams():
    from kiro_crew.teams.client import TeamsClient

    return TeamsClient(app_id="a", app_password="p", on_message=AsyncMock())


_CLIENTS = [
    pytest.param(_discord, id="discord"),
    pytest.param(_telegram, id="telegram"),
    pytest.param(_wecom, id="wecom"),
    pytest.param(_webex, id="webex"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("build", _CLIENTS)
async def test_close_closes_session_when_the_task_died_with_an_error(build) -> None:
    client = build()
    session = _FakeSession()
    client._session = session  # type: ignore[assignment]
    client._task = await _task_that_died(RuntimeError("loop blew up"))

    with pytest.raises(RuntimeError):
        await client.close()

    assert session.close_calls == 1, (
        "close() skipped the session close because awaiting the dead task "
        "re-raised; the connector and its sockets leak for the process lifetime"
    )
    assert client._session is None


@pytest.mark.asyncio
@pytest.mark.parametrize("build", _CLIENTS)
async def test_close_closes_session_when_the_task_was_cancelled(build) -> None:
    """The already-handled arm: a genuinely cancelled task must still close."""
    client = build()
    session = _FakeSession()
    client._session = session  # type: ignore[assignment]

    async def _sleep_forever() -> None:
        await asyncio.sleep(3600)

    task = asyncio.ensure_future(_sleep_forever())
    await asyncio.sleep(0)
    client._task = task

    await client.close()

    assert session.close_calls == 1
    assert client._session is None


@pytest.mark.asyncio
async def test_wecom_close_survives_a_websocket_that_refuses_to_close() -> None:
    """WeCom awaited `_ws.close()` unguarded, unlike DiscordClient.close().

    A websocket whose transport is already broken raises there, which took the
    session close down with it.
    """
    client = _wecom()
    session = _FakeSession()
    client._session = session  # type: ignore[assignment]

    class _BadWS:
        closed = False

        async def close(self) -> None:
            raise OSError("transport gone")

    client._ws = _BadWS()  # type: ignore[assignment]

    await client.close()

    assert session.close_calls == 1
    assert client._session is None


_DRAINING_CLIENTS = [
    pytest.param(_discord, id="discord"),
    pytest.param(_wecom, id="wecom"),
    pytest.param(_webex, id="webex"),
    pytest.param(_teams, id="teams"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("build", _DRAINING_CLIENTS)
async def test_close_closes_session_when_cancelled_while_draining_handlers(build) -> None:
    """Cancelling ``close()`` while it awaits the handler drain must still close.

    These clients drain in-flight handler tasks above the session close --
    Discord, WeCom and Webex inside the ``finally``, Teams with no ``finally``
    at all. That drain awaits, so a ``CancelledError`` landing
    there would exit ``close()`` with the session -- the exact leak this module
    exists to prevent -- still open, unless the close is nested in its own
    ``finally``. (Telegram is not parametrised here: its session close is the
    first awaited statement in its ``finally``.)
    """
    client = build()
    session = _FakeSession()
    client._session = session  # type: ignore[assignment]

    swallowed_once = False

    async def _stubborn_handler() -> None:
        nonlocal swallowed_once
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            if not swallowed_once:
                swallowed_once = True
                # Swallow the drain's cancel once so close() stays parked in
                # its gather until the cancellation lands on close() itself.
                await asyncio.sleep(3600)
            raise

    handler = asyncio.ensure_future(_stubborn_handler())
    await asyncio.sleep(0)
    client._handler_tasks.add(handler)

    close_task = asyncio.ensure_future(client.close())
    # Let close() run into the drain's gather before cancelling it.
    for _ in range(10):
        await asyncio.sleep(0)
    close_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await close_task

    assert session.close_calls == 1, (
        "cancelling close() while it drained handler tasks skipped the "
        "session close; the connector and its sockets leak"
    )
    assert client._session is None


# ── Teams: two owned sessions ──
#
# Teams is the only client that owns more than one session, so "the session was
# closed" is not the whole invariant for it. Both the Connector session and the
# attachment-download session must get a close attempt, and neither the drain
# above them nor a failure closing the first may cancel the second.


def _teams_with_two_sessions() -> tuple:
    client = _teams()
    session = _FakeSession()
    download = _FakeSession()
    client._session = session  # type: ignore[assignment]
    client._download_session = download  # type: ignore[assignment]
    return client, session, download


@pytest.mark.asyncio
async def test_teams_close_closes_both_sessions_when_cancelled_while_draining() -> None:
    """A cancel landing in the drain must not strand EITHER Teams session.

    The parametrised drain test above only wires ``_session``; Teams also owns
    ``_download_session``, whose connector holds the ``_VettedResolver`` and its
    SSRF pin map. Leaking it keeps those sockets and that map alive for the
    process lifetime.
    """
    client, session, download = _teams_with_two_sessions()

    swallowed_once = False

    async def _stubborn_handler() -> None:
        nonlocal swallowed_once
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            if not swallowed_once:
                swallowed_once = True
                await asyncio.sleep(3600)
            raise

    handler = asyncio.ensure_future(_stubborn_handler())
    await asyncio.sleep(0)
    client._handler_tasks.add(handler)

    close_task = asyncio.ensure_future(client.close())
    for _ in range(10):
        await asyncio.sleep(0)
    close_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await close_task

    assert session.close_calls == 1, (
        "cancelling close() during the handler drain skipped the Connector "
        "session close; its connector and sockets leak"
    )
    assert download.close_calls == 1, (
        "cancelling close() during the handler drain skipped the download "
        "session close; its connector, resolver and SSRF pin map leak"
    )


@pytest.mark.asyncio
async def test_teams_close_closes_the_download_session_when_the_first_close_raises() -> None:
    """The sibling arm the single-session clients cannot have.

    ``ClientSession.close()`` is not infallible -- its connector close can raise
    on a transport already torn down by the platform. With the two closes as
    consecutive statements, the second one simply never runs. The first failure
    must still propagate: a shutdown that swallows it reports success it did not
    achieve.
    """
    client, session, download = _teams_with_two_sessions()

    async def _bad_close() -> None:
        session.close_calls += 1
        raise OSError("connector transport gone")

    session.close = _bad_close  # type: ignore[assignment]

    with pytest.raises(OSError):
        await client.close()

    assert session.close_calls == 1
    assert download.close_calls == 1, (
        "the Connector session's close() raised and took the download "
        "session's close down with it; its sockets leak"
    )
