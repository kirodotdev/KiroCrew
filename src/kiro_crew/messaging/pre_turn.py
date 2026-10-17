"""The ordered pre-turn sequence every DM channel runs before ``drive_turn``.

A channel dispatcher's ``handle_message`` ends the same way everywhere: resolve
the session key, refuse to start a second concurrent turn, rotate the
conversation on an idle/daily boundary, then drive the turn. Three ordering
constraints make that sequence load-bearing rather than incidental, and all
three are invisible at the call site:

1. **The busy check runs BEFORE rotation.** Rotating first advances the
   generation, which mints a NEW session key -- so the in-flight turn on the old
   key is missed, ``is_busy`` reads False, and a second concurrent turn starts
   instead of the message being folded in via steer.
2. **The session key is re-derived AFTER rotation.** Rotation changed the
   generation, so the pre-rotation key addresses the conversation rotation just
   retired.
3. **Rotation must happen at all.** ``maybe_rotate`` is also what records
   activity (``last_active``), so a dispatcher that never calls it leaves that
   timestamp frozen and BOTH ``messaging.idle_reset_minutes`` and
   ``daily_reset_hour`` are silently inert -- configured, documented, and dead.

Every one of those is a "remember to do it" contract, and each has been got
wrong at least once: the shared state object (``ConversationState``) has existed
all along, but nothing forced a dispatcher to drive it in the right order. This
module turns the sequence into a single call so the ordering cannot be
re-litigated per channel.

What stays OUT, deliberately:

* **The command grammar.** Which token a channel spells ``/new`` with, and how a
  group mention is stripped before matching, remains in that channel's own
  ``commands.py`` -- the same boundary
  :mod:`kiro_crew.messaging.commands` already draws. This helper is called
  AFTER the command intercept has declined to handle the message.
* **The busy REPLY.** What a user sees when their message folds into a running
  turn is channel-shaped (a steer receipt, a "resend please" nudge), so the
  caller passes it as ``on_busy``.

Dependency direction is ``<channel> -> messaging`` (never the reverse).
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from kiro_crew.messaging.conversation import ConversationState

K = TypeVar("K")

__all__ = ["resolve_pre_turn"]


async def resolve_pre_turn(
    *,
    conv: ConversationState,
    sessions: Any,
    key: K,
    session_key_for: Callable[[K], str],
    idle_minutes: int = 0,
    daily_reset_hour: int = -1,
    on_busy: Callable[[str], Awaitable[None]],
    now: float | None = None,
) -> str | None:
    """Run the busy check, then rotation, and return the key to drive the turn on.

    Returns ``None`` when the inbound message was handed to *on_busy* -- the
    caller must return without driving a turn. Otherwise returns the
    post-rotation session key.

    ``session_key_for`` is called TWICE on purpose (once before the busy check,
    once after rotation); it must be a pure function of *key* plus the
    conversation generation, which is what makes the second call observe the
    rotation.
    """
    session_key = session_key_for(key)
    if sessions.is_busy(session_key):
        await on_busy(session_key)
        return None
    conv.maybe_rotate(
        key,
        time.time() if now is None else now,
        idle_minutes=idle_minutes,
        daily_reset_hour=daily_reset_hour,
    )
    return session_key_for(key)
