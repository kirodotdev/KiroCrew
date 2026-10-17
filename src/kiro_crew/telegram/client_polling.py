"""The ``getUpdates`` loop and the durable update cursor.

Long-polls with a capped exponential back-off and reports health through ``on_status``;
persists the low-water-mark offset under ``routing/`` (off-loop, serialized, advances
only) and drops the pre-keystone cursor. Each function here whose first parameter is
``self`` is bound as the ``TelegramClient`` attribute of the same name in
:mod:`kiro_crew.telegram.client`, which keeps the client's state, so
``self.<name>(...)`` calls and instance and class patches reach it as they reach a
method defined in the class body. They read ``_bot_id``, ``_closed``, ``_in_flight``,
``_last_status``, ``_offset``, ``_offset_path``, ``_offset_saved``, ``_polling_timeout``
and ``on_status``, write ``_last_status``, ``_offset`` and ``_offset_saved``, and update
``_in_flight`` in place. New polling or cursor work goes here.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING

import aiohttp

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import data_home

if TYPE_CHECKING:
    from kiro_crew.telegram.client import TelegramClient

logger = logging.getLogger("kiro_crew.telegram.client")


#: Consecutive polling failures before the status callback reports unhealthy.
_STATUS_FAILURE_THRESHOLD = 3

#: Ceiling on the polling loop's exponential retry delay, in seconds.
_POLL_BACKOFF_MAX_S = 30.0


#: Where the getUpdates cursor lives. ``routing`` is the keystone-registered
#: directory the Teams routing store already uses; see ``TelegramClient.__init__``
#: for why an intake cursor belongs behind the same fence as delivery addressing.
_CURSOR_DIRNAME = "routing"
_CURSOR_FILENAME = "telegram_offset.json"

#: The pre-keystone location, in the data-home root where the agent could write it.
#: Removed rather than migrated: a cursor at a writable path is exactly the
#: "cursor we cannot trust" that ``_load_offset`` already answers 0 for in five
#: other cases, and carrying its value forward would carry a poisoned one forward
#: too. The cost is the one bounded replay that module documents as the accepted
#: degradation -- Telegram redelivers what it still holds.
_LEGACY_CURSOR_FILENAME = "telegram_offset.json"


def _load_offset(self: TelegramClient) -> int:
    """The persisted getUpdates cursor, or 0 when there is none to trust.

    Every failure mode — absent file, unreadable, non-UTF-8, non-JSON, wrong
    shape, negative — answers 0, which is exactly the pre-persistence
    behaviour: Telegram redelivers what it still holds and the operator sees
    a bounded replay. Blocking; callers run it off the loop.
    """
    try:
        data = json.loads(self._offset_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return 0
    if not isinstance(data, dict):
        return 0
    # A cursor from a DIFFERENT bot is not ours to resume; start clean rather
    # than apply another bot's id space. An older file with no recorded bot is
    # also refused: we cannot tell whose it is.
    if data.get("bot_id") != self._bot_id:
        return 0
    value = data.get("offset")
    # bool is an int subclass, and True would read as offset 1.
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _discard_legacy_offset(self: TelegramClient) -> None:
    """Remove a pre-keystone cursor from the data-home root. Never raises.

    Not migrated. A cursor at a path the agent can write is the same
    "cursor we cannot trust" that :meth:`_load_offset` answers 0 for in five
    other cases, and reading it forward would read a poisoned value forward
    with it. Deleting it costs one bounded replay on the first boot after the
    upgrade, which is the degradation that method already documents.

    Only ever removes the exact legacy filename in the data-home ROOT, so it
    cannot touch the live cursor now under ``routing/``.
    """
    legacy = data_home() / _LEGACY_CURSOR_FILENAME
    if legacy == self._offset_path:
        return
    try:
        legacy.unlink(missing_ok=True)
    except OSError:
        logger.debug("Telegram: could not remove the legacy offset file", exc_info=True)


def _save_offset(self: TelegramClient, offset: int) -> None:
    """Persist the cursor atomically. Blocking; callers run it off the loop.

    A write failure is logged and swallowed: a read-only or full data home
    must not stop message delivery, and the cost is one bounded replay window
    on the next restart rather than a dead channel.
    """
    try:
        self._offset_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(
            self._offset_path,
            json.dumps({"bot_id": self._bot_id, "offset": offset}),
        )
    except OSError:
        logger.debug("Telegram: offset persist failed", exc_info=True)


def _notify_status(self: TelegramClient, healthy: bool, reason: str) -> None:
    """Invoke the health callback on state CHANGE, swallowing its errors.

    Deduplicated on the last reported state so the polling loop can call
    it unconditionally on every successful poll — only actual transitions
    (healthy↔unhealthy) reach the callback.
    """
    if self.on_status is None or self._last_status == healthy:
        return
    self._last_status = healthy
    try:
        self.on_status(healthy, reason)
    except Exception:
        logger.debug("Telegram on_status callback failed", exc_info=True)


def _poll_backoff(self: TelegramClient, attempt: int, reason: str) -> float:
    """The retry delay for consecutive-failure *attempt*, reporting unhealthy
    once the streak reaches ``_STATUS_FAILURE_THRESHOLD``.

    Called on EVERY failure with the ALREADY-incremented count, so the first
    one waits a second. *reason* reaches the status callback only on the
    attempt that crosses the threshold, which is what keeps a single blip
    from flipping the settings badge.
    """
    if attempt == _STATUS_FAILURE_THRESHOLD:
        self._notify_status(False, reason)
    return min(1.0 * (2 ** (attempt - 1)), _POLL_BACKOFF_MAX_S)


async def _polling_loop(self: TelegramClient) -> None:
    """Long-polling loop with exponential backoff on failure."""
    attempt = 0
    while not self._closed:
        try:
            updates = await self._get_updates()
            if updates is None:
                # API-level failure (ok:false — 401 bad token, 409 conflict,
                # etc). _api already logged it; back off like a transport
                # error instead of hot-looping getUpdates with zero delay.
                attempt += 1
                await asyncio.sleep(
                    self._poll_backoff(
                        attempt, "getUpdates rejected by Telegram (check the bot token)"
                    )
                )
                continue
            # Deduped in _notify_status: only an actual unhealthy→healthy
            # transition (incl. recovery from an offline boot) fires.
            self._notify_status(True, "")
            attempt = 0  # reset on success
            for update in updates:
                self._dispatch(update)
            # AFTER the whole batch is dispatched, so every update that became
            # a live handler is already registered in flight and the low-water
            # mark holds behind the oldest of them. An empty batch, or one whose
            # updates were all of a kind nothing handles, advances the cursor
            # here — which is what stops an undeliverable update replaying for
            # the life of the install.
            self._maybe_persist_offset()
        except asyncio.CancelledError:
            break
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            if self._closed:
                break
            attempt += 1
            delay = self._poll_backoff(
                attempt, f"getUpdates transport error ({type(exc).__name__})"
            )
            # Log only the exception type — an aiohttp exc's str() can embed
            # the request URL, which contains the bot token (a registered
            # credential). Mirrors _api's transport-error logging.
            logger.warning(
                "Telegram polling error (%s), retry in %.1fs",
                type(exc).__name__,
                delay,
            )
            await asyncio.sleep(delay)
        except Exception:
            if self._closed:
                break
            logger.exception("Telegram polling unexpected error")
            await asyncio.sleep(5.0)


async def _get_updates(self: TelegramClient) -> list[dict] | None:
    """Call getUpdates with long-poll timeout."""
    params = {
        "offset": self._offset,
        "timeout": self._polling_timeout,
        "allowed_updates": ["message", "callback_query"],
    }
    # record=False: the long-poll deliberately blocks for ~polling_timeout
    # (30s default) and runs back-to-back forever, so recording it would bury
    # the outbound send/edit distribution the metric exists to measure under
    # a permanent ~30000ms mode. The Telemetry surface does not split on the
    # `method` attribute (see _OTHER_SPLIT_ATTRS), so filtering here is the
    # only way to keep the percentiles meaningful.
    result = await self._api("getUpdates", params, timeout=self._polling_timeout + 10, record=False)
    if result is None:
        return None  # API-level failure — signal the polling loop to back off
    # result is the array of Update objects ([] when there are none).
    if isinstance(result, list):
        for upd in result:
            uid = upd.get("update_id", 0)
            if uid >= self._offset:
                self._offset = uid + 1
        # The in-memory cursor advances on OBSERVATION, because that is what the
        # next getUpdates call has to send to avoid refetching the batch. It is
        # deliberately NOT persisted here: at this point the batch has not been
        # dispatched, so nothing is registered in flight and the low-water mark
        # would equal the observed cursor — writing it would be the same
        # lose-the-message bug in a different place. The polling loop persists
        # once the whole batch is registered.
        return result
    return []


def _resolve_updates(self: TelegramClient, update_ids: "Iterable[int]") -> None:
    """Mark *update_ids* handled, and persist the cursor if that advanced it.

    Called from a handler task's ``finally``, so it runs whether the turn
    answered, was refused by authorization, or raised. All three are TERMINAL:
    the update will never be worth redelivering, which is exactly what the
    cursor is allowed to skip past.
    """
    self._in_flight.difference_update(update_ids)
    self._maybe_persist_offset()


def _persistable_offset(self: TelegramClient) -> int:
    """The cursor it is SAFE to resume from: the oldest unresolved update.

    WHAT THIS DOES NOT DO, stated first because the name invites the wrong
    reading: it is **not** a delivery guarantee across a crash. ``getUpdates``
    is Telegram's own acknowledgement — calling it with ``offset=N`` confirms
    everything below N SERVER-SIDE, and there is no API call to un-confirm. So
    once the loop polls again, an in-flight update is gone from Telegram
    whatever this file says, and a crash mid-turn loses that message. Closing
    that would mean either serializing inbound behind turn completion (a long
    turn would then block ``/stop``) or durably storing the update PAYLOAD
    before confirming it, which is a different feature: a persistent inbound
    queue. This cursor bounds DUPLICATE REPLAY, not loss.

    Within that scope, two failure modes have to be avoided at once:

    * Persisting what was OBSERVED replays nothing but re-answers nothing
      either: on restart the process resumes past updates it never handled, so
      any that Telegram had NOT yet confirmed are skipped too.
    * Persisting only what COMPLETED replays forever. An update the gateway
      deliberately never turns into a turn (a sticker, an unauthorized sender)
      has no completion to wait for, so a strict high-water mark on success
      would redeliver it on every restart for the life of the install.

    A low-water mark resolves both, because "resolved" covers refusal as well as
    delivery: advance to the observed cursor when nothing is in flight, and hold
    at the oldest in-flight id otherwise. The replay window is then exactly the
    set of turns that were still running — the smallest honest answer, and a
    strict improvement on tracking nothing, which is what this channel did
    before.
    """
    if not self._in_flight:
        return self._offset
    return min(self._in_flight)


def _maybe_persist_offset(self: TelegramClient) -> None:
    """Persist the safe cursor when it advanced. Fire-and-forget, off the loop."""
    if self._persistable_offset() <= self._offset_saved:
        return
    self._track(asyncio.create_task(self._persist_offset()))


async def _persist_offset(self: TelegramClient) -> None:
    """Write the safe cursor, serialized and monotonic.

    Two concurrent turns finishing hand two writes to the thread pool, and
    nothing orders those threads: the lower value can land last and the file
    REGRESSES, which on the next restart replays turns that had already been
    answered. Both halves are needed to stop that, and neither alone is enough:

    * the lock serializes the writes, so two are never in the pool at once;
    * the value is recomputed INSIDE the lock, and refused if it is not an
      advance. Recomputing is what makes the write current rather than whatever
      was true when the task was created, and the monotonic guard is a property
      of the file rather than an assumption about scheduling — so it holds even
      if a future caller reaches this from somewhere the lock does not cover.
    """
    async with self._offset_lock:
        safe = self._persistable_offset()
        if safe <= self._offset_saved:
            return
        self._offset_saved = safe
        # A small write on a slow disk still stalls every other task on this
        # single loop, so it goes to a thread.
        await asyncio.to_thread(self._save_offset, safe)
