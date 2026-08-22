"""A transport client's ``close()`` must always close its aiohttp session.

Each of the four long-lived messaging clients (Discord, Telegram, WeCom, Webex)
owns one ``aiohttp.ClientSession`` and one background task, and each ``close()``
cancels the task, awaits it, and then closes the session.

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
assert the session was closed anyway and the exception still propagated.
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
