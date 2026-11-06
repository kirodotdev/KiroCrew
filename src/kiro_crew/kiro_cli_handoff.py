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

The invariant every reader and writer of this state keeps:

1. **The handover owns the chat from** :func:`hold` **until its record is
   gone.** While the chat is *handed over* -- held, in either spelling of its
   key, or an open's record still on disk (:func:`handover_refusal`) -- only the
   handover itself (the release's settle step, or the recovery that reads the
   record) adds a turn to the chat's transcript, rewrites its history or
   changes its native conversation (a notice row is none of these). Every
   other writer (a send, a queued or injected turn, a pre-spawn, a
   session allocation, a rewind, regenerate, variant switch or edit-resend, a
   conversation discard) first brings the record's turns in where it can
   (``recover_left_open``) and otherwise refuses, changing nothing; its last
   check runs in the same yield-free step as its commit.
2. **Only the holder ends a hold.** The open takes it; it is dropped only by
   :func:`release` naming the holder's terminal: after the settle step at the
   terminal's teardown, or straight away for a terminal that never started.
   Once the settle step runs, that early release is a no-op, and a recovery
   never settles an open whose hold is live.

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
    "LEFT_OPEN_PENDING_REFUSAL",
    "forget_open",
    "handover_refusal",
    "has_left_open",
    "hold",
    "holder_of",
    "is_held",
    "is_handed_over",
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
    "Exit kiro-cli in that terminal tab (type /quit there, or close the tab), "
    "then send it again: this chat picks up "
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


def _key(session_key: str) -> str:
    """*session_key* in its canonical spelling, the one every hold is kept
    under: a chat reached by its legacy bare Slack key and by ``slack:<thread>``
    is one chat, so a hold taken under either spelling must be seen, and
    released, under both."""
    from kiro_crew.messaging.link import canonical_key

    return canonical_key(session_key) if session_key and isinstance(session_key, str) else ""


def is_held(session_key: str) -> bool:
    """Whether *session_key* (in either spelling) is open in kiro-cli in a terminal."""
    key = _key(session_key)
    with _lock:
        return bool(key) and key in _holds


#: :func:`is_held`, which already reads both spellings; kept for its callers.
is_held_any_spelling = is_held


#: The refusal a dashboard message meets when an open's record is still on disk
#: after the recovery that should have brought its turns in (it failed, and
#: keeps the record to retry): taking the message now would put it in the
#: transcript ahead of older turns made in kiro-cli.
LEFT_OPEN_PENDING_REFUSAL = (
    "What was said in kiro-cli while this chat was open there is not in the chat "
    "yet, so this message did not go through. Send it again in a moment: the chat "
    "brings those turns in first, then continues."
)

#: The refusal for a session an open left a record of (see the module docstring):
#: the chat's own turn brings those turns in first, so it is the way on.
LEFT_OPEN_REFUSAL = (
    "Kiro Crew restarted while this chat was open in kiro-cli, and what was said "
    "there is not in the chat yet. Send the chat a message: it brings those turns "
    "in, then continues."
)


def handover_refusal(session_key: str) -> tuple[str, str] | None:
    """``(message, code)`` refusing a writer other than the handover while
    *session_key* is handed over (see the module docstring), else ``None``.
    Blocking (one stat), so it can sit in the same yield-free step as the
    write it guards."""
    if is_held(session_key):
        return HELD_TURN_REFUSAL, "kiro_cli_open"
    if has_left_open(session_key):
        return LEFT_OPEN_PENDING_REFUSAL, "kiro_cli_import_pending"
    return None


def is_handed_over(session_key: str) -> bool:
    """Whether *session_key* is held or an open's record of it is on disk.
    Blocking (one stat)."""
    return handover_refusal(session_key) is not None


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
        held = _holds.get(_key(session_key))
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
    key = _key(session_key)
    if not key or not terminal_id:
        return False
    with _lock:
        if key in _holds:
            return False
        _holds[key] = _Hold(terminal_id, on_release, before_release)
    return True


def release(session_key: str, terminal_id: str) -> bool:
    """Drop *terminal_id*'s hold on *session_key*; True when one was dropped.

    Only the holder can release, so a late teardown of an earlier terminal can
    never free a hold a newer one took.
    """
    key = _key(session_key)
    with _lock:
        held = _holds.get(key)
        if held is None or held.terminal_id != terminal_id:
            return False
        del _holds[key]
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
        held = _holds.get(_key(session_key))
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
    from kiro_crew.owner_only_files import mkdirs_owner_only

    path = _record_path(session_key)
    # Every directory it creates owner-only, not only the last one.
    mkdirs_owner_only(path.parent)
    atomic_write(path, json.dumps({**record, "session_key": session_key}), mode=0o600)


def forget_open(session_key: str, *, import_id: str = "") -> None:
    """Remove the record of *session_key*'s open, if any. Blocking; never raises.

    With *import_id*, only a record that open wrote goes: the record is keyed by
    session, so a late cleanup of an earlier open must not remove the one a
    newer open of the same chat wrote meanwhile. A record that cannot be read is
    then left in place, for its own open or recovery to settle."""
    path = _record_path(session_key)
    if import_id:
        try:
            with open(path, encoding="utf-8") as fh:
                record = json.load(fh)
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            logger.warning("kiro-cli handoff: could not read an open's record", exc_info=True)
            return
        if not isinstance(record, dict) or record.get("import_id") != import_id:
            return
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("kiro-cli handoff: could not remove an open's record", exc_info=True)


def has_left_open(session_key: str) -> bool:
    """Whether a record of an open of *session_key* is on disk, readable or not.
    Blocking (one stat). A record that cannot be read still counts: turns may be
    waiting behind it."""
    return bool(_key(session_key)) and os.path.lexists(_record_path(session_key))


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
