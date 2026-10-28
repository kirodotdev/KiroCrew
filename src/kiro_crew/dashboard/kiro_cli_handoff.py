"""Which chat sessions are open in kiro-cli in the built-in terminal right now.

"Open in kiro-cli" hands a chat's kiro-cli conversation to a terminal tab that
runs ``kiro-cli chat --resume-id <sid>``. Two processes must never write one
kiro-cli session at once, so while that terminal holds the conversation the
chat's own agent process is gone (the open resets it, keeping the resume
pointer) and nothing may start another one for the same session key: a
dashboard turn refuses at admission and the eager pre-spawn stands down. When
the terminal's process exits, its session teardown releases the hold, and the
chat's next turn cold-starts and ``session/load``\\ s the conversation from the
log kiro-cli wrote, turns made in the terminal included.

The record is in memory only, on purpose: the terminal's process is a child of
this gateway and does not outlive it, so a hold surviving a restart would only
lock a chat whose terminal is already gone.

A leaf module: the turn path, the eager spawn and the terminal lifecycle all
read it, and it imports none of them.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

__all__ = [
    "HELD_TURN_REFUSAL",
    "hold",
    "holder_of",
    "is_held",
    "release",
    "reset_holds",
    "settle_and_release",
]

#: The error row a dashboard turn leaves when its session is open in kiro-cli.
HELD_TURN_REFUSAL = (
    "This chat is open in kiro-cli in the terminal, so it was not sent here. "
    "Exit kiro-cli in that terminal tab, then send it again: this chat picks up "
    "the conversation, including what you did in kiro-cli."
)


#: How long a release waits for its transcript step before releasing anyway.
SETTLE_TIMEOUT_S = 10.0


@dataclass
class _Hold:
    terminal_id: str
    on_release: Callable[[], None] | None
    before_release: Callable[[], Awaitable[None]] | None = None


# session key -> hold. A thread lock rather than none: reads come from the event
# loop, but a test or a teardown in a worker must never see a half-written map.
_holds: dict[str, _Hold] = {}
_lock = threading.Lock()


def is_held(session_key: str) -> bool:
    """Whether *session_key* is open in kiro-cli in a terminal."""
    with _lock:
        return bool(session_key) and session_key in _holds


def holder_of(session_key: str) -> str:
    """The terminal session id holding *session_key*, or ``""``."""
    with _lock:
        held = _holds.get(session_key)
        return held.terminal_id if held is not None else ""


def hold(
    session_key: str,
    terminal_id: str,
    *,
    on_release: Callable[[], None] | None = None,
    before_release: Callable[[], Awaitable[None]] | None = None,
) -> bool:
    """Record that *terminal_id* holds *session_key*; False when one already does.

    *on_release* runs once, when :func:`release` drops this hold.
    *before_release* is awaited by :func:`settle_and_release` before it does.
    """
    if not session_key or not terminal_id:
        return False
    with _lock:
        if session_key in _holds:
            return False
        _holds[session_key] = _Hold(terminal_id, on_release, before_release)
    return True


def release(session_key: str, terminal_id: str) -> bool:
    """Drop *terminal_id*'s hold on *session_key*; True when one was dropped.

    Only the holder can release, so a late teardown of an earlier terminal can
    never free a hold a newer one took.
    """
    with _lock:
        held = _holds.get(session_key)
        if held is None or held.terminal_id != terminal_id:
            return False
        del _holds[session_key]
    if held.on_release is not None:
        try:
            held.on_release()
        except Exception:  # noqa: BLE001 - a notice must never block the teardown
            logger.warning("kiro-cli handoff: release callback failed", exc_info=True)
    return True


async def settle_and_release(session_key: str, terminal_id: str) -> bool:
    """Release *terminal_id*'s hold once its *before_release* step has run.

    The terminal's teardown calls this after the kiro-cli process is gone. The
    step (bringing kiro-cli's turns into the chat's transcript) runs while the
    hold still stands, so no chat turn can start between kiro-cli exiting and
    the transcript catching up. A step that fails or overruns
    :data:`SETTLE_TIMEOUT_S` is logged and the hold is released anyway: a stuck
    import must never leave the chat refusing every turn.
    """
    with _lock:
        held = _holds.get(session_key)
        step = held.before_release if held is not None and held.terminal_id == terminal_id else None
    if step is not None:
        try:
            await asyncio.wait_for(step(), timeout=SETTLE_TIMEOUT_S)
        except asyncio.CancelledError:
            # A cancelled teardown still frees the chat.
            release(session_key, terminal_id)
            raise
        except Exception:  # noqa: BLE001 - see the docstring
            logger.warning("kiro-cli handoff: settling the transcript failed", exc_info=True)
    return release(session_key, terminal_id)


def reset_holds() -> None:
    """Forget every hold without running callbacks (test isolation)."""
    with _lock:
        _holds.clear()
