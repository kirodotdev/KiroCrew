"""Is this channel a code channel, and is it OURS?

``conversations.info`` answers it: ``properties.record_channel.record_type``
is ``"agent_channel"`` on a code channel and on nothing else, and
``properties.agent_session.encoded_agent_bot_user_ids`` names the agents Slack
assigned to it. A code channel is ours when that set holds this gateway's bot
user id -- which covers a channel opened from Slack's own UI with this app as
its agent, not only the ones this gateway created.

The answer is re-read on EVERY inbound message for a code channel: Slack can
remove an agent from a channel at any time, and a stale "ours" would keep
answering in a channel it does not belong to. Only an ORDINARY answer is
cached, briefly, so ordinary channels do not cost a lookup per message.
A lookup that fails reads as UNKNOWN, never as ordinary or ours.
"""

from __future__ import annotations

import enum
import logging
import time
from collections import OrderedDict

from kiro_crew.slack.code_channels.client import CodeChannelsClient
from kiro_crew.slack.code_channels.models import ChannelInfo, CodeChannelApiError

logger = logging.getLogger(__name__)

ORDINARY_CACHE_SECS = 30.0
ORDINARY_CACHE_MAX = 4096


class ChannelKind(enum.Enum):
    OURS = "ours"
    OTHER_AGENT = "other_agent"
    ORDINARY = "ordinary"
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"


_ordinary: OrderedDict[str, float] = OrderedDict()


def could_be_code_channel(channel_id: str) -> bool:
    """Only public/private channels can be code channels; DMs never are."""
    return bool(channel_id) and channel_id[0] in ("C", "G")


def forget(channel_id: str) -> None:
    _ordinary.pop(channel_id, None)


def clear_cache() -> None:
    _ordinary.clear()


def _cached_ordinary(channel_id: str) -> bool:
    expires = _ordinary.get(channel_id)
    if expires is None:
        return False
    if expires < time.monotonic():
        _ordinary.pop(channel_id, None)
        return False
    return True


def _remember_ordinary(channel_id: str) -> None:
    _ordinary[channel_id] = time.monotonic() + ORDINARY_CACHE_SECS
    _ordinary.move_to_end(channel_id)
    while len(_ordinary) > ORDINARY_CACHE_MAX:
        _ordinary.popitem(last=False)


async def classify_channel(
    client: CodeChannelsClient | None, channel_id: str, self_user_id: str
) -> tuple[ChannelKind, ChannelInfo | None]:
    """Read *channel_id*'s kind (see the module docstring)."""
    if not could_be_code_channel(channel_id):
        return ChannelKind.ORDINARY, None
    if _cached_ordinary(channel_id):
        return ChannelKind.ORDINARY, None
    if client is None:
        return ChannelKind.UNKNOWN, None
    try:
        info = await client.channel_info(channel_id)
    except CodeChannelApiError as exc:
        if exc.unavailable:
            return ChannelKind.UNAVAILABLE, None
        return ChannelKind.UNKNOWN, None
    if not info.is_code_channel:
        _remember_ordinary(channel_id)
        return ChannelKind.ORDINARY, info
    if not self_user_id:
        # Our own identity is unverified, so "ours" cannot be told from "theirs".
        return ChannelKind.UNKNOWN, info
    if self_user_id in info.agent_bot_user_ids:
        return ChannelKind.OURS, info
    return ChannelKind.OTHER_AGENT, info
