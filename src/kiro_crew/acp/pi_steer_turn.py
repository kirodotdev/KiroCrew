"""pi's mid-turn steer on ``AcpClient``: the sealed-file channel to Crew's gate extension.

The channel itself, and the security boundary it keeps, is
:mod:`kiro_crew.acp.harness.pi_steer`. This module is the client's half: it opens the
channel for each prompt turn, writes a steer only while that turn is open, reads the
extension's consumed notification back out of the turn's stream, and removes every
steer file when the turn closes.

Every function here takes the client as its first argument and keeps its state in
the fields ``AcpClient.__init__`` creates: ``_pi_steer_turn`` (the open turn's request
id, or ``None``), ``_pi_steers_pending`` (sequence number -> the wrapped text the
caller's pending entry is matched by) and ``_pi_steer_lock`` (serializes a write
against the turn's close, so no file is written after its turn closed). Only the
channel is read with a default, because every client reaches these entry points and
only a pi launch mints one.

Fields read: ``_pi_steer_channel``, ``_session_id``, ``_turn_done``, ``_cancelled``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from kiro_crew.acp.harness.pi_steer import steer_echo_seq
from kiro_crew.acp.types import (
    EVENT_STEER_CONSUMED,
    MAX_STEERING_ANSWERS,
    MAX_STEERING_TEXT_CHARS,
    UPDATE_AGENT_MESSAGE_CHUNK,
    AcpEvent,
    JsonRpcMessage,
)

if TYPE_CHECKING:
    from kiro_crew.acp.client import AcpClient

logger = logging.getLogger("kiro_crew.acp.client")


def ready(client: AcpClient) -> bool:
    """Whether this session's pi launch minted a steer channel."""
    return getattr(client, "_pi_steer_channel", None) is not None


def open_turn(client: AcpClient, req_id: int) -> None:
    """Open the channel for prompt turn *req_id*: steers written from now aim at it."""
    if not ready(client):
        return
    client._pi_steer_turn = req_id
    client._pi_steers_pending.clear()


async def close_turn(client: AcpClient, req_id: int) -> None:
    """Close turn *req_id*: no steer is written for it after this, and none is left.

    Every steer file the channel wrote is removed, whether the extension read it or
    not: one it did not read by now is the caller's to queue, and must not be read
    into a later turn. Under the write lock, so a write in flight lands first and is
    removed with the rest.
    """
    channel = getattr(client, "_pi_steer_channel", None)
    if channel is None:
        return
    async with client._pi_steer_lock:
        if client._pi_steer_turn not in (req_id, None):
            # A later turn is open (this close is a late ``finally``): its files
            # are its own, and its terminal removes them.
            return
        client._pi_steer_turn = None
        client._pi_steers_pending.clear()
        try:
            await asyncio.to_thread(channel.discard)
        except OSError as exc:
            logger.warning("pi steer files not removed: %s", type(exc).__name__)


async def _undo_write(channel: Any, write: asyncio.Future[int], seq: int) -> None:
    """Wait out a write its caller stopped waiting for, then remove what it wrote.

    Every further cancel is absorbed until both are done, still under the write lock,
    so the file cannot land after the lock is released and outlive its turn.
    """
    while not write.done():
        with suppress(asyncio.CancelledError):
            await asyncio.shield(write)
    if not write.cancelled():
        write.exception()  # retrieved, so a failed write never logs as unhandled
    remove = asyncio.ensure_future(asyncio.to_thread(channel.remove, seq))
    while not remove.done():
        with suppress(asyncio.CancelledError):
            await asyncio.shield(remove)
    if not remove.cancelled() and remove.exception() is not None:
        logger.warning("pi steer file not removed: %s", type(remove.exception()).__name__)


async def steer(client: AcpClient, text: str) -> bool:
    """Write *text* as a steer for the open turn; True when it was written.

    True means the extension can read it before the turn's next model call, not that
    it has: the extension's consumed notification is what reports it
    (:func:`echo_event`), and a steer the turn ends without reading is left to the
    caller's pending entry, which queues it (``steer_needs_loss_recovery``). False --
    no open turn, a cancelled or ended one, text over the bound, too many steers
    already waiting, or a write that failed -- sends the caller down its queue path.
    """
    channel = getattr(client, "_pi_steer_channel", None)
    if channel is None:
        return False
    aimed_at = client._pi_steer_turn
    if aimed_at is None or not client._session_id:
        return False
    if len(text) > MAX_STEERING_TEXT_CHARS:
        return False
    wrapped = f"<user_message>\n{text}\n</user_message>"
    async with client._pi_steer_lock:
        pending = client._pi_steers_pending
        if (
            client._pi_steer_turn != aimed_at
            or client._turn_done.is_set()
            or client._cancelled
            or len(pending) >= MAX_STEERING_ANSWERS
        ):
            return False
        # Registered BEFORE the write: the read loop can see the extension's
        # notification before this coroutine resumes from the thread.
        expected = channel.seq + 1
        pending[expected] = wrapped
        write = asyncio.ensure_future(asyncio.to_thread(channel.publish, client._session_id, text))
        try:
            # Shielded: the thread runs on if this call is cancelled, so the write
            # is waited out here, still under the lock, and undone.
            await asyncio.shield(write)
        except asyncio.CancelledError:
            pending.pop(expected, None)
            await _undo_write(channel, write, expected)
            raise
        except Exception as exc:
            pending.pop(expected, None)
            logger.info("pi steer not written: %s", type(exc).__name__)
            return False
    client._last_steer_monotonic = time.monotonic()
    return True


def echo_event(client: AcpClient, msg: JsonRpcMessage) -> AcpEvent | None | bool:
    """Read the extension's consumed notification for a steer of this turn.

    ``False`` when *msg* is not one (the caller handles it as before). Otherwise the
    frame is the extension's own and carries no text for the turn: ``None`` when it
    names no steer still pending, or ``EVENT_STEER_CONSUMED`` with the pending
    entry's wrapped text. Only a pi-acp NOTIFY chunk counts -- an
    ``agent_message_chunk`` carrying ``_meta.piAcp.notify``, which only an extension
    dialog produces, never model text -- and only with this launch's nonce.
    """
    channel = getattr(client, "_pi_steer_channel", None)
    if channel is None:
        return False
    params: Any = msg.params if isinstance(msg.params, dict) else {}
    update = params.get("update")
    if not isinstance(update, dict) or update.get("sessionUpdate") != UPDATE_AGENT_MESSAGE_CHUNK:
        return False
    meta = update.get("_meta")
    pi_acp = meta.get("piAcp") if isinstance(meta, dict) else None
    if not isinstance(pi_acp, dict) or "notify" not in pi_acp:
        return False
    content = update.get("content")
    text = content.get("text") if isinstance(content, dict) else None
    if not isinstance(text, str):
        return False
    seq = steer_echo_seq(text, channel.nonce)
    if seq is None:
        return False
    wrapped = client._pi_steers_pending.pop(seq, None)
    if wrapped is None:
        return None
    return AcpEvent(kind=EVENT_STEER_CONSUMED, text=wrapped)
