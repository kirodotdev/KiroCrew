"""Each code-channel turn's opening row, kept where the session's agent cannot write.

Restart resume (:mod:`kiro_crew.slack.code_channels.resume`) names the turn a
restart cut from this record alone. A transcript row and the metadata line's
turn marker are files the session's own agent can rewrite, so neither decides
that a turn may run again by itself.

At admission (``chat_turn.turn_marker``'s turn observer) the opener the
dispatcher started the turn from -- this process's own copy -- is recorded under
``dispatch:<slot key>`` in the code-channel store, before any provider work; when
the turn's marker is retired the record is removed, once its write has landed.
A process that dies mid-turn leaves it in place, naming the turn it cut.

Every start takes those records out of the store (:func:`start`, Slack or not)
before it records any of its own, and the restart reads only what it took. So a
record names a turn of the previous process alone, and only when the restored
marker names that same opener: a turn with no record of its own (the write
failed, the slot was not yet known as a code channel's, or it began before the
observer was registered) has nothing that can name it, and its work waits for the
owner's Resume.

Only code-channel sessions are recorded: a channel's own session and the workers
it created. For every other session both callbacks return at once.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from kiro_crew.slack.code_channels.store import StoreUnavailable, store

logger = logging.getLogger(__name__)

#: The store key prefix: ``dispatch:<slot key>``.
DISPATCH_PREFIX = "dispatch:"
#: How long an admission waits for its record to land before the turn runs.
WRITE_WAIT_S = 5.0

#: Slot key -> the record this process is writing (or wrote) at its admission and
#: that write, so the retirement removes exactly that one, after it lands, and
#: never a successor's.
_written: dict[str, tuple[dict[str, str], "asyncio.Future[None]"]] = {}
#: Removals in flight, held so the loop keeps them alive.
_pending: set["asyncio.Task[None]"] = set()


def _is_code_channel_slot(slot: Any) -> bool:
    """*slot* runs a code channel, or is a worker one created (loaded snapshot only)."""
    key = str(getattr(slot, "key", "") or "")
    if not key:
        return False
    try:
        sessions = store().session_keys()
        if key in sessions or store().worker_conductor(key):
            return True
    except StoreUnavailable:
        return False
    return str(getattr(slot, "_created_by", "") or "") in sessions


def _record_of(row: dict[str, Any] | None) -> dict[str, str]:
    """The opener's identity and kind; empty fields when there is no copy."""
    meta = row.get("meta") if isinstance(row, dict) else None
    meta = meta if isinstance(meta, dict) else {}
    return {
        "ts": str((row or {}).get("ts") or ""),
        "mid": str(meta.get("mid") or ""),
        "role": str((row or {}).get("role") or ""),
        "kind": str(meta.get("injectKind") or ""),
        "at": str(time.time()),
    }


async def admitted(slot: Any, row: dict[str, Any] | None) -> None:
    """Record *slot*'s turn opener before the turn runs (a code-channel slot only).

    Nothing is recorded until this start's take has landed: a retried take
    removes every ``dispatch:`` record, and must never remove this process's own.
    """
    if not _landed:
        return
    if not store().loaded:
        try:
            await asyncio.to_thread(store().load)
        except Exception:
            return
    if not _is_code_channel_slot(slot):
        return
    key = str(slot.key)
    record = _record_of(row)
    write = asyncio.ensure_future(
        asyncio.to_thread(store().record_resume, DISPATCH_PREFIX + key, record)
    )
    write.add_done_callback(lambda done: done.cancelled() or done.exception())
    # Registered before the await: a turn cancelled while the write is in flight
    # still has its record removed by its retirement, once the write lands.
    _written[key] = (record, write)
    try:
        # Bounded: a store that does not answer delays the turn by this much at
        # most. The write goes on; until it lands a restart finds no matching
        # opener and holds the turn.
        await asyncio.wait_for(asyncio.shield(write), WRITE_WAIT_S)
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        logger.warning("code channel %s: turn opener still being recorded", key)
    except Exception:
        # Not recorded: a restart that cuts this turn finds no opener matching
        # its marker, so the turn is held for the owner's Resume.
        if _written.get(key, (None, None))[1] is write:
            _written.pop(key, None)
        logger.warning("code channel %s: turn opener not recorded", key, exc_info=True)


def retired(slot: Any) -> None:
    """Remove the record this process wrote for *slot*'s turn, now that it ended."""
    key = str(getattr(slot, "key", "") or "")
    entry = _written.pop(key, None)
    if entry is None:
        return
    record, write = entry

    def _drop() -> None:
        try:
            store().replace_resume_if(DISPATCH_PREFIX + key, record, None)
        except Exception:
            # Kept: it names a turn that ended. A restart resumes it only when its
            # marker names that same opener, and then at most once.
            logger.warning("code channel %s: turn opener not removed", key, exc_info=True)

    async def _after_write() -> None:
        try:
            await write
        except Exception:
            return  # never recorded: nothing to remove
        await asyncio.to_thread(_drop)

    try:
        task = asyncio.get_running_loop().create_task(_after_write())
    except RuntimeError:  # pragma: no cover - retired off the loop (tests)
        _drop()
        return
    _pending.add(task)
    task.add_done_callback(_pending.discard)


def opener(key: str) -> dict[str, str] | None:
    """The recorded opener of *key*'s turn the last process did not finish.

    From the records :func:`taken` took out of the store at start-up: one this
    process wrote is never among them, so a restart only ever names a turn the
    previous process recorded and was cut during.
    """
    return (_left or {}).get(key)


#: Slot key -> the opener the previous process left recorded; ``None`` until taken.
_left: dict[str, dict[str, str]] | None = None
_taking: "asyncio.Task[None] | None" = None
#: Whether a take has emptied the store of the previous process's records. Until
#: it has, this process records nothing, so a later take never removes its own.
_landed = False
#: Seconds between attempts at a take whose write failed.
RETAKE_S = 30.0
_retaking: "asyncio.Task[None] | None" = None


async def _take() -> None:
    global _left, _landed, _retaking
    try:
        found = await asyncio.to_thread(store().take_resume_prefix, DISPATCH_PREFIX)
    except Exception:
        # Not taken: nothing names a cut turn, so every one waits for a person,
        # and the records stay until a retry removes them.
        logger.warning("code channel resume: recorded turn openers not taken", exc_info=True)
        _left = {}
        _retaking = asyncio.get_running_loop().create_task(_retake())
        return
    _left = {key[len(DISPATCH_PREFIX) :]: value for key, value in found.items()}
    _landed = True


async def _retake() -> None:
    """Retry the take until its write lands; what it finds names nothing now."""
    global _landed
    while not _landed:
        await asyncio.sleep(RETAKE_S)
        try:
            await asyncio.to_thread(store().take_resume_prefix, DISPATCH_PREFIX)
        except Exception:
            continue
        _landed = True


async def taken() -> None:
    """Take the previous process's records out of the store, once (idempotent).

    Taken out rather than read, so no record outlives the start-up that read it:
    a turn this process runs without recording its opener (Slack not connected,
    a failed write, a slot not yet known as a code channel's) can never be named
    later by an older record a written marker happens to match.
    """
    global _taking
    if _taking is None:
        _taking = asyncio.get_running_loop().create_task(_take())
    await asyncio.shield(_taking)


async def start() -> None:
    """At start-up: take the previous process's records, then observe this one's turns.

    The order is the point: a record this process writes is never taken. A turn
    admitted before the observer is registered records nothing, so a restart that
    cuts it holds it for the owner's Resume.
    """
    from kiro_crew.dashboard.chat_runner import register_turn_observer

    await taken()
    register_turn_observer(admitted, retired)


def reset() -> None:
    """Forget what was taken (tests: a new process)."""
    global _left, _taking, _landed, _retaking
    if _retaking is not None and not _retaking.done():
        _retaking.cancel()
    _left, _taking, _landed, _retaking = None, None, False, None
