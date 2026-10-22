"""What this process last saw of a code channel's audience: whether it is shared.

The code-channel dispatch exemption
(:mod:`kiro_crew.dashboard.session_control_parts.code_channel_exemption`) lets a
channel's own conductor hand work to workers, and what it reads back lands in the
channel. A channel shared beyond this workspace -- Slack Connect, an org-wide
share, or an external invitation still pending -- puts that in front of people
outside the organisation, so the exemption is withheld there.

The reading comes from ``conversations.info``. The inbound gate calls it for every
directed message (:func:`recognition.classify_channel`), and the exemption asks for
a fresh one itself (:func:`refresh_soon`) whenever the reading it holds is missing or
older than :data:`READING_TTL_SECS` -- a dashboard turn, a patrol wake or a slash
command never passes the inbound gate, and a share can happen between messages.
The reading is held in memory only, because the exemption gate is synchronous and
must not reach Slack, so every one of these reads "unknown" -- the gate's
fail-closed answer -- until a fresh answer lands:

* no reading yet (a restart, an evicted channel);
* a reading older than :data:`READING_TTL_SECS`;
* a lookup that FAILED after an earlier answer (:func:`forget`), so a stale "not
  shared" never outlives a lookup that could not confirm it;
* an answer that did not carry the share flags.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import OrderedDict
from typing import Any

logger = logging.getLogger(__name__)

#: Most channels remembered at once (the oldest reading is dropped first).
MAX_CHANNELS = 1024
#: How long one ``conversations.info`` answer about sharing is trusted.
READING_TTL_SECS = 300.0

_lock = threading.Lock()
_shared: OrderedDict[str, tuple[bool, float]] = OrderedDict()
_refreshing: set[str] = set()


def note_channel_info(info: Any) -> None:
    """Record what *info* (a :class:`~.models.ChannelInfo`) says about sharing.

    A reading that did not carry the share flags forgets the channel rather than
    keeping an older answer.
    """
    channel_id = str(getattr(info, "channel_id", "") or "")
    if not channel_id:
        return
    shared = getattr(info, "shared", None)
    with _lock:
        if shared is None:
            _shared.pop(channel_id, None)
            return
        _shared[channel_id] = (bool(shared), time.monotonic())
        _shared.move_to_end(channel_id)
        while len(_shared) > MAX_CHANNELS:
            _shared.popitem(last=False)


def channel_shared(channel_id: str) -> bool | None:
    """``True`` shared, ``False`` confirmed workspace-internal, ``None`` unknown or stale."""
    with _lock:
        entry = _shared.get(channel_id)
    if entry is None:
        return None
    shared, read_at = entry
    if time.monotonic() - read_at > READING_TTL_SECS:
        return None
    return shared


def forget(channel_id: str) -> None:
    """Drop the reading for *channel_id* (it reads "unknown" until the next one)."""
    with _lock:
        _shared.pop(channel_id, None)


def clear() -> None:
    """Forget every reading (tests)."""
    with _lock:
        _shared.clear()
        _refreshing.clear()


def refresh_soon(channel_id: str) -> bool:
    """Start one background ``conversations.info`` read for *channel_id*.

    Fire-and-forget on the running loop (the caller is a synchronous gate that has
    just refused for want of a fresh reading); at most one read per channel is in
    flight. Returns whether one was started. Without a loop or a live Slack client
    nothing starts and the channel keeps reading "unknown".
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    with _lock:
        if channel_id in _refreshing:
            return False
        _refreshing.add(channel_id)
    try:
        loop.create_task(_refresh(channel_id))
    except Exception:
        with _lock:
            _refreshing.discard(channel_id)
        return False
    return True


async def _refresh(channel_id: str) -> None:
    try:
        from kiro_crew.slack.code_channels import surface
        from kiro_crew.slack.code_channels.client import CodeChannelsClient

        client = CodeChannelsClient.from_slack(surface.slack())
        if client is None:
            return
        info = await client.channel_info(channel_id)
    except Exception:
        logger.debug("code channel %s: share lookup failed", channel_id, exc_info=True)
        forget(channel_id)
        return
    finally:
        with _lock:
            _refreshing.discard(channel_id)
    note_channel_info(info)
