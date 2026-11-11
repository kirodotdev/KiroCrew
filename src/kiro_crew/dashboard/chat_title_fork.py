"""When a fork child gets its own name: on the first message a person sends it.

A fork is born ``↳ Fork of <parent>`` (``↳ Tail of`` for a tail fork,
``chat_fork.fork_slot``) and keeps that name until the person sends the child a
message. That message then names it through ``chat_title.refresh_forked_title``,
read together with the turns before it; a question copied from the parent never
starts the pass. This module decides when that one pass starts; ``chat_title``
owns the pass itself.
"""

from __future__ import annotations

import asyncio
from typing import Any

from kiro_crew.dashboard.chat_title import (
    FORK_TITLE_MARKER,
    _counts_as_user_turn,
    _refresh_blocked,
    refresh_forked_title,
)
from kiro_crew.dashboard.prompt_handoff import turn_handed_off
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.history import HUMAN_TURN_META_KEY

# The names a fork child is born with: the marker and one of these words in
# front of the parent's title.
_FORK_BIRTH_PREFIXES = (f"{FORK_TITLE_MARKER}Fork of ", f"{FORK_TITLE_MARKER}Tail of ")


def fork_title_pending(slot: _ChatSlot) -> bool:
    """True while a fork child still waits for its first message to name it.

    Read from the title itself, so a gateway restart before the first message
    still names the fork. Only an AUTO title qualifies: a fork of a manually
    named parent, or one given an explicit title, keeps its name. One pass per
    process lifetime (``_fork_title_spent``), so a SKIP or a failed generation
    is not retried on every later message.
    """
    return (
        not slot._fork_title_spent
        and not _refresh_blocked(slot)
        and slot.title.startswith(_FORK_BIRTH_PREFIXES)
    )


def _is_human_row(message: dict[str, Any]) -> bool:
    meta = message.get("meta")
    return isinstance(meta, dict) and meta.get(HUMAN_TURN_META_KEY) is True


def _last_turn_is_human(slot: _ChatSlot) -> bool:
    """True when the newest user row in *slot* is one a person typed."""
    for message in reversed(slot.messages):
        if message.get("role") == "user":
            return _is_human_row(message)
    return False


def schedule_fork_title(state: DashboardState, slot: _ChatSlot) -> None:
    """Start the fork child's title pass from the message just sent to it.

    Called by the send handler (``chat_api.chat_send``) right after it appends
    a person's message to a slot for which ``fork_title_pending`` holds, so the
    name lands during the turn, after its prompt handoff, as a new session's
    first title does. The task
    handle goes in ``_title_task`` so chat_done's ``title_then_refresh`` waits
    for it before the milestone refresh reads the title.
    """
    slot._fork_title_spent = True
    task = asyncio.create_task(_name_fork_after_handoff(state, slot))
    slot._title_task = task
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)


async def _name_fork_after_handoff(state: DashboardState, slot: _ChatSlot) -> None:
    """The on-send pass: it waits for the turn that send started to hand off."""
    await turn_handed_off(slot)
    await refresh_forked_title(state, slot)


async def name_queued_fork(state: DashboardState, slot: _ChatSlot) -> None:
    """Name a fork child at chat_done when its first message came through the queue.

    A queued message never passed the on-send trigger, so ``title_then_refresh``
    calls this before the milestone refresh. The on-send pass, when it ran,
    already spent the fork's one pass, and this returns without work.

    A chat_done that added no user row leaves a copied parent question as the
    newest person-typed row. ``fork_slot`` set ``_title_refresh_mark`` to the
    copied user-turn count, so no new row means no pass.
    """
    if not (fork_title_pending(slot) and _last_turn_is_human(slot)):
        return
    user_count = sum(1 for m in slot.messages if _counts_as_user_turn(slot, m))
    if user_count <= slot._title_refresh_mark:
        return
    slot._fork_title_spent = True
    await refresh_forked_title(state, slot)
