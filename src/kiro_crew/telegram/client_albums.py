"""Album (media group) coalescing for inbound Telegram messages.

Telegram delivers an album as one ``message`` update per member, sharing a
``media_group_id``; the members are buffered and flushed as ONE merged message after an
idle window or a hard ceiling, so a four-picture album is one turn. Each function here
whose first parameter is ``self`` is bound as the ``TelegramClient`` attribute of the
same name in :mod:`kiro_crew.telegram.client`, which keeps the client's state, so
``self.<name>(...)`` calls and instance and class patches reach it as they reach a
method defined in the class body. They update ``_albums``, ``_album_timers``,
``_album_first_seen``, ``_album_dropped`` and ``_album_updates`` in place. New album
handling goes here.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.telegram.client import TelegramClient, TelegramInbound

logger = logging.getLogger("kiro_crew.telegram.client")


# ── Album (media group) coalescing ──
# Telegram delivers an album as N separate `message` updates sharing one
# `media_group_id`, with the caption on only one member. We buffer members and
# emit ONE merged message so a four-screenshot album is one turn, not four.
#: Idle gap after the last member before flushing. Album members arrive
#: back-to-back (typically in a single getUpdates batch), so this only has to
#: outlast intra-batch jitter -- not a user's typing.
_ALBUM_WINDOW_S = 1.0
#: Hard ceiling from the FIRST member, so a stream that keeps appending to one
#: group can never defer the flush indefinitely.
_ALBUM_MAX_WAIT_S = 5.0
#: Per-group member cap. Telegram's own album limit is 10, so this is only
#: reachable via a malformed/spoofed stream; it keeps the buffer bounded.
_ALBUM_MAX_MEMBERS = 10
#: Concurrent buffered groups. Defence-in-depth: each group self-flushes within
#: _ALBUM_MAX_WAIT_S, so this only matters under a burst of incomplete groups.
_ALBUM_MAX_GROUPS = 64


def _buffer_album_member(
    self: TelegramClient, group_id: str, inbound: TelegramInbound, *, update_id: int = 0
) -> None:
    """Hold one album member and (re)arm its flush timer.

    The timer is rearmed on every arrival, so the album flushes
    ``_ALBUM_WINDOW_S`` after the LAST member rather than the first — album
    members arrive back-to-back (usually in one getUpdates batch), so this
    settles almost immediately. ``_ALBUM_MAX_WAIT_S`` is the hard ceiling
    that stops a pathological stream which keeps appending to one group from
    deferring the flush forever.
    """
    members = self._albums.get(group_id)
    if members is None:
        # Cap concurrent groups. Every group self-flushes within
        # _ALBUM_MAX_WAIT_S, so this is defence-in-depth against a burst of
        # never-completed groups rather than an expected path. Flush the
        # oldest rather than dropping it, so no message is silently lost.
        if len(self._albums) >= _ALBUM_MAX_GROUPS:
            oldest = min(self._albums, key=lambda g: self._album_first_seen.get(g, 0.0))
            logger.warning(
                "Telegram: album buffer at %d groups, force-flushing oldest",
                _ALBUM_MAX_GROUPS,
            )
            self._flush_album(oldest)
        members = self._albums[group_id] = []
        self._album_first_seen[group_id] = time.monotonic()

    if update_id:
        self._album_updates.setdefault(group_id, []).append(update_id)
    if len(members) < _ALBUM_MAX_MEMBERS:
        members.append(inbound)
    else:
        # Telegram's own album limit is 10, so this is unreachable for a
        # well-formed album. Count rather than grow, and surface it at flush
        # so an over-cap group is visible instead of silently truncated.
        self._album_dropped[group_id] = self._album_dropped.get(group_id, 0) + 1

    self._arm_album_timer(group_id)


def _arm_album_timer(self: TelegramClient, group_id: str) -> None:
    """(Re)schedule the flush for *group_id*, respecting the hard ceiling."""
    existing = self._album_timers.pop(group_id, None)
    if existing is not None and not existing.done():
        existing.cancel()
    elapsed = time.monotonic() - self._album_first_seen.get(group_id, 0.0)
    delay = min(_ALBUM_WINDOW_S, max(0.0, _ALBUM_MAX_WAIT_S - elapsed))
    task = asyncio.create_task(self._album_flush_after(group_id, delay))
    self._album_timers[group_id] = task
    # Tracked alongside handler tasks so a pending flush is not garbage
    # collected mid-flight.
    self._track(task)


async def _album_flush_after(self: TelegramClient, group_id: str, delay: float) -> None:
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return  # a newer member rearmed the timer
    self._album_timers.pop(group_id, None)
    self._flush_album(group_id)


def _flush_album(self: TelegramClient, group_id: str) -> None:
    """Merge one buffered album into a single message and dispatch it."""
    pending = self._album_updates.pop(group_id, [])
    members = self._albums.pop(group_id, None)
    self._album_first_seen.pop(group_id, None)
    dropped = self._album_dropped.pop(group_id, 0)
    timer = self._album_timers.pop(group_id, None)
    if timer is not None and not timer.done():
        timer.cancel()
    if not members:
        # Nothing to deliver, so every id this group was holding is resolved —
        # otherwise an emptied group would pin the cursor for the process's life.
        self._resolve_updates(pending)
        return

    # Usually the caption rides on exactly one member, but Telegram Desktop
    # and Android let the user caption individual items of a media group --
    # so join every non-empty caption in album order rather than taking the
    # first. For the single-caption case this is identical; for the
    # per-item case it is the difference between the model seeing all of
    # the user's words and silently seeing only the first.
    # Everything else comes from the first member: its message_id is what a
    # reply or a steer-ack reaction should target.
    head = members[0]
    text = "\n\n".join(m.text for m in members if m.text)
    attachments: list[dict[str, Any]] = []
    for member in members:
        attachments.extend(member.attachments)
    if dropped:
        logger.warning(
            "Telegram: album %s exceeded %d members; %d ignored",
            group_id,
            _ALBUM_MAX_MEMBERS,
            dropped,
        )
    # Derived from the HEAD rather than enumerated field by field. An album is
    # the head message with more photos and a joined caption, so the only things
    # the merge decides are those two — everything else is the head's identity
    # and has to survive verbatim. Enumerating meant a field added to
    # TelegramInbound was silently dropped here: `reply_to_user_id` went missing
    # that way, and a reply-to-the-bot album in a mention-mode forum Topic was
    # then discarded by the activation gate with no trace. `replace` carries a
    # new field by construction, so the class cannot recur.
    # `mentions` and `has_entities` join that short list because both describe
    # the TEXT, which the merge does decide: a mention in a non-head member's
    # caption is in the joined text, so keeping only the head's would let
    # mention-only activation drop an album that addressed the bot.
    merged = replace(
        head,
        text=text,
        attachments=attachments,
        mentions=tuple(dict.fromkeys(h for m in members for h in m.mentions)),
        has_entities=any(m.has_entities for m in members),
    )
    # The ids go WITH the merged message rather than being resolved here:
    # _spawn_handler only creates a task, so acking at this point would advance
    # the cursor past an album whose turn has not run. The handler resolves them
    # as a unit in its finally, because replaying half an album would deliver the
    # same photos again under a caption that does not match.
    self._spawn_handler(merged, tuple(pending))


def _flush_all_albums(self: TelegramClient) -> None:
    """Best-effort flush of every buffered album, used on shutdown.

    **Not a delivery guarantee.** Shutdown runs the channel teardown and
    ``SessionManager.close_all()`` concurrently in one ``cleanup_tasks``
    gather, and ``close_all`` sets ``_closing``, after which ``begin_turn``
    raises ``SessionClosingError``. So a handler spawned here may lose the
    race and be refused.

    Kept anyway because it is free and sometimes wins, and because the
    residual is not a new failure mode: a plain single message that arrives
    just before shutdown is refused by that same ``_closing`` gate today.
    Buffering an album widens that pre-existing window by at most
    ``_ALBUM_WINDOW_S``; it does not introduce a class of loss that was not
    already there. Draining the buffer here also keeps a closed client from
    holding album state.
    """
    for group_id in list(self._albums):
        self._flush_album(group_id)
