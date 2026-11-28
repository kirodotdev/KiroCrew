"""Rename a Discord thread the bot opened so it reads as its session's title.

A thread the bot opens from a message in an allowed channel is named from the
first 90 characters of that message (``DiscordTransport.receive``), while Slack
retitles its thread once the conversation has a name. :func:`rename_bot_thread`
is the Discord half of that: the auto-title turn calls it with the name it
generated, and a ``!title`` command can call it with the name a person typed.

What it refuses, and why:

* **A thread the bot did not open.** Asked of Discord itself on every rename
  (:meth:`DiscordClient.thread_origin`, uncached), because a thread a person
  opened and then allow-listed is theirs to name.
* **A destination the operator withdrew.** The name is visible to everyone who can
  see the thread, and a naming turn finishes long after the reply did, so the
  ``channels`` ceiling and the live rosters are read again right before the
  write, the same re-check the REST ladder runs after a wait.
* **A name already applied.** One rename per title change, never one per turn.
* **A name that went stale.** A caller passes ``still_current`` when the name
  belongs to one conversation of the thread (a ``!new`` replaces it); it is read
  right before the write and after every REST back-off.

A name is model- or person-written and shown in the server's thread list, so it
loses line breaks, control and format characters (bidi overrides, zero-width
marks), each turned into a space rather than deleted so no two pieces of the
title are joined into something an earlier redaction never saw, and is cut to
Discord's 100-character limit.

Every failure is quiet and leaves the turn as it was. Discord's Modify Channel
docs ask ``MANAGE_THREADS`` for a thread edit; the bot is not invited with it
(it would also let the bot read every private thread and delete or lock any
thread), so a rename relies on a thread's opener being allowed to rename it,
which Discord does not document (discord-api-docs#5706). A 403 is logged once
for that thread, which is then not tried again until a restart; a rate limit or
any other error is logged and the next title change tries again.
"""

from __future__ import annotations

import logging
import unicodedata
from collections import OrderedDict
from typing import TYPE_CHECKING, Any, Callable

from kiro_crew.sel import sel

if TYPE_CHECKING:
    from kiro_crew.discord.client import DiscordClient

logger = logging.getLogger(__name__)

#: Discord's limit on a channel or thread name.
THREAD_NAME_MAX = 100

#: Unicode categories a name drops: control, format, surrogate, private use.
_DROPPED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co"})

#: Bound on each per-thread memory below; eviction is least recently written.
_BOUND = 4096

#: thread id -> the name this process last gave it. Thread ids are global
#: snowflakes, so one table serves every client.
_applied: OrderedDict[str, str] = OrderedDict()

#: thread id -> None for every thread Discord refused to rename (403).
_refused: OrderedDict[str, None] = OrderedDict()


def _remember(table: OrderedDict[str, Any], thread_id: str, value: Any) -> None:
    table[thread_id] = value
    table.move_to_end(thread_id)
    while len(table) > _BOUND:
        table.popitem(last=False)


def thread_name(title: str) -> str:
    """*title* as a Discord thread name, or ``""`` when nothing printable is left.

    Whitespace of every kind (line and paragraph separators included) becomes one
    space, and so does every character in :data:`_DROPPED_CATEGORIES`: replaced,
    never deleted, so the pieces of a credential a caller's redaction saw apart
    (a token split by control characters) are never joined back together. The
    result is cut to :data:`THREAD_NAME_MAX` characters.
    """
    spaced = "".join(
        " " if ch.isspace() or unicodedata.category(ch) in _DROPPED_CATEGORIES else ch
        for ch in str(title or "")
    )
    return " ".join(spaced.split())[:THREAD_NAME_MAX].rstrip()


def reset() -> None:
    """Forget every applied name and refusal. For tests only."""
    _applied.clear()
    _refused.clear()


async def rename_bot_thread(
    client: DiscordClient,
    thread_id: str,
    title: str,
    *,
    still_current: Callable[[], bool] | None = None,
) -> bool:
    """Rename *thread_id* to *title* when the bot opened it; True when it did.

    Never raises: a thread keeping its old name is a cosmetic loss, and the
    callers run after the answer was delivered.
    """
    name = thread_name(title)
    if not thread_id or not name or thread_id in _refused or _applied.get(thread_id) == name:
        return False
    try:
        bot_user_id = str(getattr(client, "bot_user_id", "") or "")
        origin = await client.thread_origin(thread_id)
        if origin is None or not bot_user_id or origin.owner_id != bot_user_id:
            logger.debug("discord: not renaming thread %s, which the bot did not open", thread_id)
            return False
        # Last before the write: the lookup above is an await, so a roster or
        # ceiling read taken before it may describe a withdrawn destination.
        permission = await client._destination_still_permitted(f"/channels/{thread_id}")
        if not permission.permitted:
            logger.info("discord: not renaming thread %s: %s", thread_id, permission.detail)
            return False
        if still_current is not None and not still_current():
            logger.debug("discord: not renaming thread %s to a stale name", thread_id)
            return False
        outcome = await client.rename_thread(thread_id, name, still_current=still_current)
    except Exception:
        logger.warning("discord: renaming thread %s failed", thread_id, exc_info=True)
        return False
    if not outcome:
        if outcome.status == 403:
            _remember(_refused, thread_id, None)
            logger.info(
                "discord: Discord refused renaming thread %s; it keeps its name (granting "
                "the bot Manage Threads there allows it)",
                thread_id,
            )
        else:
            logger.info(
                "discord: renaming thread %s failed (%s %s)",
                thread_id,
                outcome.status,
                outcome.detail,
            )
        return False
    _remember(_applied, thread_id, name)
    sel().log_api_access(
        caller="system",
        operation="discord.thread_rename",
        outcome="allowed",
        source="discord",
        resources=f"thread={thread_id}",
    )
    return True
