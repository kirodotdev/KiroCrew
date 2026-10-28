"""Which chat sessions are open in kiro-cli in the built-in terminal right now.

"Open in kiro-cli" hands a chat's kiro-cli conversation to a terminal tab that
runs ``kiro-cli chat --resume-id <sid>``. Two processes must never write one
kiro-cli session at once, so while that terminal holds the conversation the
chat's own agent process is gone (the open resets it, keeping the resume
pointer) and nothing may start another one for the same session key: a
dashboard turn refuses at admission, the eager pre-spawn stands down, and
every other caller -- a channel that resumed or later linked the chat, a cron,
a sub-agent delivery -- is refused where sessions are allocated
(``SessionManager.get_or_create`` calls :func:`refuse_if_held`). When
the terminal's process exits, its session teardown releases the hold, and the
chat's next turn cold-starts and ``session/load``\\ s the conversation from the
log kiro-cli wrote, turns made in the terminal included.

The hold is in memory only, on purpose: the terminal's process is a child of
this gateway and does not outlive it, so a hold surviving a restart would only
lock a chat whose terminal is already gone. What a restart must not lose is the
turns made in that terminal, which the release brings into the chat. So each
open also leaves a small record on disk (:func:`record_open`), removed with the
hold once those turns are durably in the chat; a record still there after a
restart (or a failed import) is turns not yet brought in, and the chat's next
turn reads it (:func:`read_left_open`) and brings them in first.

A leaf module: the session manager, the turn path, the eager spawn and the
terminal lifecycle all read it, and it imports none of them.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "HELD_TURN_REFUSAL",
    "forget_open",
    "has_left_open",
    "hold",
    "holder_of",
    "is_held",
    "is_held_any_spelling",
    "read_left_open",
    "record_open",
    "refuse_if_held",
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


def is_held_any_spelling(session_key: str) -> bool:
    """:func:`is_held` for *session_key* as given or in its canonical spelling."""
    if is_held(session_key):
        return True
    from kiro_crew.messaging.link import canonical_key

    return is_held(canonical_key(session_key))


#: The refusal for a session an open left a record of (see the module docstring):
#: the chat's own turn brings those turns in first, so it is the way on.
LEFT_OPEN_REFUSAL = (
    "Kiro Crew restarted while this chat was open in kiro-cli, and what was said "
    "there is not in the chat yet. Send the chat a message: it brings those turns "
    "in, then continues."
)


def refuse_if_held(session_key: str) -> None:
    """Raise a fixed-wording, non-retryable ``AcpError`` when *session_key* is
    held, or an open of it left a record a restart kept from being released, so
    a channel or other non-dashboard caller says why instead of a generic
    failure. The one allocation choke point calls this. Blocking (one stat)."""
    if is_held_any_spelling(session_key):
        message = HELD_TURN_REFUSAL
    elif has_left_open(session_key):
        message = LEFT_OPEN_REFUSAL
    else:
        return
    # Imported here: the ACP layer is heavy, and this module stays a leaf.
    from kiro_crew.agent_sdk.drivers.acp_vocab import AcpError

    raise AcpError(message, transient=False, user_worded=True)


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


# ── the on-disk record of each open, for a restart ──────────────────────────

#: The folder holding one record per open; see the module docstring. It sits in
#: the data home's ``run`` subtree, which every sandboxed agent sees read-only:
#: a record names files the gateway removes and an envelope state it writes back,
#: so an agent that could plant one could steer those writes.
RECORDS_DIRNAME = "kiro-cli-opens"


def _records_dir() -> Path:
    from kiro_crew.config.paths import data_home

    return data_home() / "run" / RECORDS_DIRNAME


def _record_path(session_key: str) -> Path:
    from kiro_crew.messaging.link import canonical_key

    digest = hashlib.sha256(canonical_key(session_key).encode("utf-8")).hexdigest()[:32]
    return _records_dir() / f"{digest}.json"


def record_open(session_key: str, record: dict) -> None:
    """Write the record of an open of *session_key*: what its release would
    import (a JSON object). Blocking. Raises ``OSError`` when it cannot be written."""
    from kiro_crew.atomic_write import atomic_write

    path = _record_path(session_key)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    atomic_write(path, json.dumps({**record, "session_key": session_key}), mode=0o600)


def forget_open(session_key: str) -> None:
    """Remove the record of *session_key*'s open, if any. Blocking; never raises."""
    try:
        os.unlink(_record_path(session_key))
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("kiro-cli handoff: could not remove an open's record", exc_info=True)


def has_left_open(session_key: str) -> bool:
    """Whether a record of an open of *session_key* is on disk, readable or not.
    Blocking (one stat). A record that cannot be read still counts: turns may be
    waiting behind it."""
    return bool(session_key) and os.path.lexists(_record_path(session_key))


def read_left_open(session_key: str) -> dict | None:
    """The record of an open of *session_key* whose turns have not been brought
    into the chat durably yet (the gateway stopped while the terminal held it,
    or the import failed); ``None`` when there is none or the open is live
    here. Blocking. The record stays on disk until its importer calls
    :func:`forget_open` after a durable write, so a crash or a failed import
    leaves it to retry from; the import is idempotent, so a retry adds nothing
    twice. A record that does not read as one is removed: nothing could act on it.
    A record that is there but cannot be read raises ``OSError`` rather than
    reading as none.
    """
    if is_held_any_spelling(session_key):
        return None
    path = _record_path(session_key)
    try:
        with open(path, encoding="utf-8") as fh:
            record = json.load(fh)
    except FileNotFoundError:
        return None
    except OSError:
        # Raised, not read as "no record": the file is there, so turns may be
        # waiting behind it, and a new open must not replace it.
        logger.warning("kiro-cli handoff: could not read an open's record", exc_info=True)
        raise
    except ValueError:
        logger.warning("kiro-cli handoff: an open's record is not JSON; removed", exc_info=True)
        forget_open(session_key)
        return None
    if not isinstance(record, dict):
        forget_open(session_key)
        return None
    return record
