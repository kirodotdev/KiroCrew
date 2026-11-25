"""The runtime a Conductor thread shares with a code channel (RFC part D).

A Conductor thread is a code-channel binding whose room is ONE THREAD of an
ordinary channel or DM (``Binding.thread``, kept in the store's
``thread_bindings``). Worker approval cards, Stop and restart resume are the
code-channel paths that must also serve it, and they gated on
:func:`.binding.code_channels_on`, which needs the code-channel scopes a thread
does not use. This module is the one place they ask instead:

* :func:`room_runtime_on` is the gate wherever a binding is known: the
  code-channel gate for a channel binding, ``slack.thread_conductor`` for a
  thread binding (:func:`.binding.thread_conductor_on`, which also stays off
  until this runtime is built).
* A **room id** names a binding in a key or a button: the channel id for a code
  channel, ``<channel>:<anchor>`` for a thread (one channel can hold many).
  Channel ids hold no ``:``, so the two never collide.
* :func:`thread_open` and :func:`sync_gates` are the thread's counterparts of the
  code channel's "feature on, not archived" and owner-gate checks, reading only
  the store's loaded snapshot.

Owner-only throughout: every gate here asks ``is_owner`` by identity, never the
allow-list or the shared users. Nothing here widens a code channel: a channel
binding takes the code-channel branch of every function untouched.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from kiro_crew.slack.code_channels import binding as _binding
from kiro_crew.slack.code_channels import surface
from kiro_crew.slack.code_channels.binding import code_channels_on, thread_conductor_on
from kiro_crew.slack.code_channels.store import Binding, store, thread_key

logger = logging.getLogger(__name__)

#: Joins a thread's channel and anchor in a room id.
ROOM_SEP = ":"

#: "No config passed": read the live one. An explicit ``None`` is an unreadable
#: config, which reads as off.
_LIVE: Any = object()


def is_thread(binding: Any) -> bool:
    """Whether *binding* is a Conductor thread's (not a code channel's)."""
    return getattr(binding, "thread", False) is True


def room_id(binding: Binding) -> str:
    """The key that names *binding*: its channel, or its thread's ``channel:anchor``."""
    return (
        thread_key(binding.channel_id, binding.anchor_ts)
        if is_thread(binding)
        else binding.channel_id
    )


def channel_of_room(room: str) -> str:
    """The Slack channel a room id lives in."""
    return room.split(ROOM_SEP, 1)[0]


def post_thread_ts(binding: Binding) -> str | None:
    """The ``thread_ts`` this binding's posts carry: its anchor for a thread, else top level."""
    return binding.anchor_ts if is_thread(binding) and binding.anchor_ts else None


def binding_for_room(room: str) -> Binding | None:
    """The stored binding *room* names, or None. Reads the store (loads it once)."""
    if not room:
        return None
    channel, sep, anchor = room.partition(ROOM_SEP)
    if sep:
        return store().get_thread_binding(channel, anchor) if channel and anchor else None
    return store().get_binding(room)


def room_runtime_on(
    binding: Binding, cfg: Any = _LIVE, *, channels_on: Callable[[Any], bool] = code_channels_on
) -> bool:
    """The runtime gate for *binding*: code channels for a channel, the thread switch for a thread.

    *channels_on* is the code-channel gate to ask (a module that owns a patched
    name for it passes its own).
    """
    cfg = _binding.live_config() if cfg is _LIVE else cfg
    return thread_conductor_on(cfg) if is_thread(binding) else channels_on(cfg)


def any_runtime_on(
    cfg: Any = _LIVE, *, channels_on: Callable[[Any], bool] = code_channels_on
) -> bool:
    """Whether either runtime is on: the gate for a site that runs BEFORE its binding is known.

    Once the binding is in hand the site asks :func:`room_runtime_on` instead.
    """
    cfg = _binding.live_config() if cfg is _LIVE else cfg
    return channels_on(cfg) or thread_conductor_on(cfg)


#: How long a posting or a click waits for Slack's answer about a thread's room.
ROOM_READ_TIMEOUT_SECS = 5.0


async def prime_room(binding: Binding) -> None:
    """Make sure a Conductor thread's room has a fresh Slack reading before its synchronous gates run.

    The room rules (private, not shared out) read a short-lived cache the gates
    never fill (they cannot await), so a worker's card or an owner's click more
    than a few minutes after the last reading would otherwise be refused for want
    of one. A bounded read fills it; a failed or slow read leaves the reading
    unknown and the gates refuse, as for any unreadable room. A channel binding
    and a DM need no reading.
    """
    import asyncio

    from kiro_crew.slack.code_channels import audience
    from kiro_crew.slack.code_channels.store import ROOM_IM

    if not is_thread(binding) or binding.room == ROOM_IM:
        return
    if audience.channel_private(binding.channel_id) is not None:
        return
    try:
        await asyncio.wait_for(audience._refresh(binding.channel_id), ROOM_READ_TIMEOUT_SECS)
    except Exception:
        logger.debug("conductor thread: room reading not refreshed", exc_info=True)


def live_thread_binding(binding: Binding) -> Binding | None:
    """The stored row of *binding*'s thread while it is still THIS session's and not ended.

    Synchronous, from the loaded snapshot (``None`` before it is loaded): a thread
    that was ended, rebound to another session or never stored reads as None.
    """
    try:
        loaded = store().loaded_thread_bindings()
    except Exception:
        logger.debug("conductor thread: store unreadable", exc_info=True)
        return None
    for current in loaded or ():
        if current.channel_id == binding.channel_id and current.anchor_ts == binding.anchor_ts:
            if current.session_key == binding.session_key and not current.ended_at:
                return current
            return None
    return None


def conductor_thread_of(slot: Any, cfg: Any = _LIVE) -> Binding | None:
    """The live Conductor thread whose conductor *slot* is, or None.

    Synchronous, from the loaded snapshot: the thread switch on, and a stored, not
    ended thread binding at the slot's own Slack link (channel and thread) that
    names *slot*. The room rules are left to the click's owner gates, which read
    a fresh room reading first.
    """
    cfg = _binding.live_config() if cfg is _LIVE else cfg
    if not thread_conductor_on(cfg):
        return None
    channel = getattr(slot, "_slack_channel", None)
    anchor = getattr(slot, "_slack_thread_ts", None)
    key = getattr(slot, "key", None)
    if not (isinstance(channel, str) and channel and isinstance(anchor, str) and anchor):
        return None
    if not (isinstance(key, str) and key):
        return None
    try:
        loaded = store().loaded_thread_bindings()
    except Exception:
        logger.debug("conductor thread: store unreadable", exc_info=True)
        return None
    for current in loaded or ():
        if (
            current.channel_id == channel
            and current.anchor_ts == anchor
            and current.session_key == key
            and is_thread(current)
            and not current.ended_at
        ):
            return current
    return None


def room_refusal(binding: Binding) -> str:
    """Why a thread's room does not admit its runtime now (``""`` when it does).

    The dispatch exemption's own channel-state clause (ended, workspace, channel
    policy, room private or opted in, not shared out): one reading, so a card or
    a notice is posted into exactly the rooms dispatch is judged for.
    """
    try:
        from kiro_crew.dashboard.session_control_parts.code_channel_exemption import (
            _channel_state_refusal,
        )

        return str(_channel_state_refusal(binding) or "")
    except Exception:
        logger.debug("conductor thread: room check failed", exc_info=True)
        return "the room could not be checked"


def thread_open(binding: Binding, cfg: Any = _LIVE) -> bool:
    """A thread's counterpart of "the feature is on and the channel is not archived".

    The switch is on, the stored thread is still this session's and not ended,
    and its room still admits it (public only with the opt-in, never shared out).
    """
    cfg = _binding.live_config() if cfg is _LIVE else cfg
    if not thread_conductor_on(cfg):
        return False
    current = live_thread_binding(binding)
    return current is not None and not room_refusal(current)


def sync_gates(orch: Any, binding: Binding, user: str, team: str) -> bool:
    """A thread's counterpart of the code channel's synchronous owner gates.

    Workspace admitted, *user* the owner by identity, the thread open
    (:func:`thread_open`), its channel admitting the bot and the per-channel
    policy ceiling. No await and no disk read, so it can run inside a mutation's
    commit section.
    """
    from kiro_crew.slack.code_channels.routing import channel_and_origin_permit
    from kiro_crew.slack.handler import is_owner

    cfg = _binding.live_config(orch)
    return bool(
        surface.workspace_admitted(team)
        and is_owner(user)
        and thread_open(binding, cfg)
        and channel_and_origin_permit(cfg, binding.channel_id, binding.channel_id)
        and not surface.channel_policy_refusal(
            binding.channel_id, operation="slack.code_channel.control", caller=user
        )
    )


async def conductor_binding_for_slot(state: Any, slot_key: str, cfg: Any = _LIVE) -> Binding | None:
    """The code channel OR Conductor thread bound to *slot_key*, when it still names it.

    The channel's own lookup first (:func:`.surface.binding_for_slot`); a thread
    binding only while the thread switch is on. Like it, a binding counts only
    while the live Slack link still resolves to that very slot in that channel.
    """
    import asyncio

    if not slot_key:
        return None
    cfg = _binding.live_config() if cfg is _LIVE else cfg
    if code_channels_on(cfg):
        found = await surface.binding_for_slot(state, slot_key)
        if found is not None:
            return found
    if not thread_conductor_on(cfg):
        return None
    for binding in await asyncio.to_thread(store().thread_bindings):
        if binding.session_key != slot_key or binding.ended_at:
            continue
        slot = _binding.bound_slot(state, binding)
        if slot is None or getattr(slot, "key", "") != slot_key:
            continue
        linked = getattr(slot, "_slack_channel", None)
        if isinstance(linked, str) and linked and linked != binding.channel_id:
            continue  # the anchor ts belongs to some other channel's link
        return binding
    return None


def thread_words(text: str) -> str:
    """*text*, said about a code channel, reworded for a thread ("this channel" to "this thread")."""
    for channel_form, thread_form in (
        ("this channel's", "this thread's"),
        ("This channel's", "This thread's"),
        ("this channel", "this thread"),
        ("This channel", "This thread"),
    ):
        text = text.replace(channel_form, thread_form)
    return text


def say(binding: Binding, text: str) -> str:
    """*text* as it should read for *binding*: reworded for a thread, unchanged for a channel."""
    return thread_words(text) if is_thread(binding) else text
