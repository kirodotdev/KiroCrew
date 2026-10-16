"""The channel's native status, driven by the session's turns.

``processing`` shows Slack's "Working..." loader (and its Stop button, which
reaches us as ``agent_session_stopped``); ``active`` means ready for the next
prompt. Slack drops ``processing`` back to ``active`` after an hour, so a turn
that runs longer re-sends it on a timer until it ends.

Installed as ordinary :mod:`.hooks` subscribers by :func:`install`. A status
write that fails is logged and ignored: the status is decoration on a turn
that runs either way.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from kiro_crew.slack.code_channels import hooks
from kiro_crew.slack.code_channels.client import CodeChannelsClient
from kiro_crew.slack.code_channels.models import CodeChannelApiError
from kiro_crew.slack.code_channels.store import Binding

logger = logging.getLogger(__name__)

#: Re-send ``processing`` this often (Slack resets it after 60 minutes).
REFRESH_SECS = 50 * 60

_client_factory: Callable[[], CodeChannelsClient | None] | None = None
_refreshers: dict[str, asyncio.Task[None]] = {}


def set_client_factory(factory: Callable[[], CodeChannelsClient | None] | None) -> None:
    """Where status writes get their client (set by :func:`install`, tests)."""
    global _client_factory
    _client_factory = factory


def _client() -> CodeChannelsClient | None:
    return _client_factory() if _client_factory is not None else None


async def set_status(channel_id: str, status: str) -> bool:
    client = _client()
    if client is None:
        return False
    try:
        await client.set_channel_status(channel_id, status)
        return True
    except CodeChannelApiError as exc:
        logger.info("code channel %s status %s not set: %s", channel_id, status, exc.code)
        return False


async def _refresh(channel_id: str) -> None:
    while True:
        await asyncio.sleep(REFRESH_SECS)
        await set_status(channel_id, "processing")


async def _on_start(binding: Binding, _event: hooks.TurnEvent) -> None:
    await set_status(binding.channel_id, "processing")
    previous = _refreshers.pop(binding.channel_id, None)
    if previous is not None:
        previous.cancel()
    _refreshers[binding.channel_id] = asyncio.ensure_future(_refresh(binding.channel_id))


async def _on_end(binding: Binding, _event: hooks.TurnEvent) -> None:
    task = _refreshers.pop(binding.channel_id, None)
    if task is not None:
        task.cancel()
    await set_status(binding.channel_id, "active")


def install(slack_provider: Callable[[], Any]) -> None:
    """Subscribe the status hooks; *slack_provider* returns the live Slack client."""
    set_client_factory(lambda: CodeChannelsClient.from_slack(slack_provider()))
    hooks.on_turn_start(_on_start)
    hooks.on_turn_end(_on_end)
