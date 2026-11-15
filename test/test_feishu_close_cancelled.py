"""A Feishu close() cut off by its timeout still releases the SDK receiver.

The channel lifecycle bounds ``close()`` with ``wait_for``. A disconnect that stalls
(the SDK holds its lock through a reconnect) is cancelled there, and the receiver loop
and executors must still be released. Otherwise the old client reconnects on its own
and keeps answering a channel that was disabled or moved to new credentials.
"""

from __future__ import annotations

import asyncio
import sys

import pytest
from test_feishu_client import _fake_lark_sdk  # noqa: F401 -- autouse SDK stub
from test_feishu_client import _FakeWSClientWithoutStop

from kiro_crew.testing.wait import async_wait_until


class _FakeWSClientStalledDisconnect(_FakeWSClientWithoutStop):
    """A disconnect stuck behind the SDK's reconnect lock: it never finishes."""

    async def _disconnect(self) -> None:
        await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_a_close_cut_off_by_its_timeout_still_releases_the_receiver() -> None:
    from kiro_crew.feishu.client import LarkClient, LarkInbound

    lark_mod = sys.modules["lark_oapi"]
    original_client = lark_mod.ws.Client  # type: ignore[attr-defined]
    lark_mod.ws.Client = _FakeWSClientStalledDisconnect  # type: ignore[attr-defined]
    try:
        delivered: list[object] = []

        async def _on_message(inbound: object) -> None:
            delivered.append(inbound)

        client = LarkClient(app_id="a", app_secret="s", on_message=_on_message)
        await client.start()
        await async_wait_until(
            client._ws_client._started_event.is_set,
            describe=lambda: "the SDK client never started on its thread",
        )

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(client.close(), timeout=0.2)

        await async_wait_until(
            lambda: not client._thread.is_alive(),
            describe=lambda: "the receiver loop was left running",
        )
        assert client._executor._shutdown is True
        client._deliver(
            LarkInbound(open_id="u", text="hi", message_id="m", chat_type="p2p", chat_id="")
        )
        assert delivered == []
    finally:
        lark_mod.ws.Client = original_client  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_a_delivery_queued_before_close_does_not_run_after_it() -> None:
    from kiro_crew.feishu.client import LarkClient, LarkInbound

    delivered: list[object] = []

    async def _on_message(inbound: object) -> None:
        delivered.append(inbound)

    client = LarkClient(app_id="a", app_secret="s", on_message=_on_message)
    client._loop = asyncio.get_running_loop()
    # Queued from the receiver's side while still open, then closed before the
    # loop gets to run it.
    client._deliver(
        LarkInbound(open_id="u", text="hi", message_id="m", chat_type="p2p", chat_id="")
    )
    client._closed = True
    # Queued the same way, so it runs only after the delivery above has run.
    marker = asyncio.run_coroutine_threadsafe(_noop(), asyncio.get_running_loop())
    await async_wait_until(marker.done, describe=lambda: "the loop never ran the queue")
    assert delivered == []


async def _noop() -> None:
    return None
