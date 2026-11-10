"""Frames for a dashboard socket wait behind its connect snapshot.

``_serve_ws`` registers a socket as soon as its handshake is answered, before it
takes the slot list its connect snapshot carries. The tab sends its catch-up reads
(notifications, approvals, questions, workflow runs, the open transcript) the moment
the socket opens, and a change made after such a read is answered reaches the tab only
as a frame, so the socket must be in the registry before any read is served. Taking
the list can wait for a shared pass (``slot_list_pass``), and the snapshot is the
socket's first frame by contract. So from :func:`hold_frames` until the snapshot is
queued, the hub keeps every frame it would send the socket (:func:`held`), in order,
and :func:`release_held` starts their sends right behind the snapshot's. An awaited
owner send keeps its frame the same way (:func:`held_send`) and is handed the send the
release starts for it, so it counts the frame only once that send completes, and a
send that fails reaches it as the failure. A socket that leaves the registry while still held (:func:`drop_held`)
ends those waits as sends to a closed socket, and its frames are discarded.

The hold ends when the snapshot is queued, not when the tab has read it: from then on
every frame reaches the socket's buffer, so a tab that stops reading is dropped by the
hub's stall check as before. A held frame can be older than the snapshot it follows.
It lands the way a frame that raced an HTTP reply always could: a whole value is
followed by the frame of its next change, and the client reconciles the rest against
its own reads.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: The frames held for a socket that waits for its connect snapshot, each with the
#: future its awaited owner send waits on (``None`` for any other frame); ``None`` after.
HELD_FRAMES_KEY = "_frames_held_for_snapshot"

#: The sends a release started, kept referenced until they finish.
_RELEASED: set[asyncio.Future[None]] = set()


def hold_frames(ws: Any) -> None:
    """Hold every hub frame for *ws* until :func:`release_held`."""
    ws[HELD_FRAMES_KEY] = []


def held(ws: Any, msg: str | bytes) -> bool:
    """Keep *msg* when *ws* still waits for its connect snapshot; True when kept."""
    frames = ws.get(HELD_FRAMES_KEY)
    if not isinstance(frames, list):
        return False
    frames.append((msg, None))
    return True


def held_send(ws: Any, msg: str | bytes) -> asyncio.Future[asyncio.Future[None]] | None:
    """Keep *msg* like :func:`held`, for a caller that awaits its send.

    Returns None when *ws* is not waiting for its connect snapshot. Otherwise returns
    a future :func:`release_held` resolves with the send it starts for *msg*, so the
    caller waits on that send as on one it started itself. A caller that stops waiting
    before the release cancels the future; *msg* still goes out in its place.
    """
    frames = ws.get(HELD_FRAMES_KEY)
    if not isinstance(frames, list):
        return None
    started: asyncio.Future[asyncio.Future[None]] = asyncio.get_running_loop().create_future()
    frames.append((msg, started))
    return started


def release_held(ws: Any) -> list[asyncio.Future[None]]:
    """Stop holding for *ws* and start a send for each held frame, in order.

    Called right after the snapshot's send was started, so each of these sends is
    queued behind it, and every later frame behind them. Returns the sends started;
    a failed one is logged, never raised here, and reaches the awaited owner send of
    its frame, if it has one, through the send handed to it.
    """
    frames = ws.get(HELD_FRAMES_KEY)
    ws[HELD_FRAMES_KEY] = None
    if not isinstance(frames, list):
        return []
    sends: list[asyncio.Future[None]] = []
    for msg, started in frames:
        send = asyncio.ensure_future(
            ws.send_str(msg) if isinstance(msg, str) else ws.send_bytes(msg)
        )
        _RELEASED.add(send)
        send.add_done_callback(_released_done)
        sends.append(send)
        # Cancelled when its caller stopped waiting first; the frame still goes out.
        if started is not None and not started.done():
            started.set_result(send)
    return sends


def drop_held(ws: Any) -> None:
    """Forget the frames held for *ws*, which is leaving the registry unreleased.

    A connect that ends before its snapshot is queued (a cancelled handler) never
    reaches :func:`release_held`. Cancelling each pending ``started`` future ends
    the awaited owner send that waits on it now, as a send to a closed socket,
    rather than at ``WS_OWNER_SEND_MAX_S``. A socket holding nothing is untouched.
    """
    get = getattr(ws, "get", None)
    frames = get(HELD_FRAMES_KEY) if callable(get) else None
    if not isinstance(frames, list):
        return
    ws[HELD_FRAMES_KEY] = None
    for _msg, started in frames:
        if started is not None and not started.done():
            started.cancel()


def _released_done(send: asyncio.Future[None]) -> None:
    _RELEASED.discard(send)
    if not send.cancelled() and send.exception() is not None:
        logger.debug("a frame held for a connect snapshot was not sent", exc_info=send.exception())
