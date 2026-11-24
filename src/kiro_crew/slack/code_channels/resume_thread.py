"""A Conductor thread's share of restart resume (RFC part D).

:mod:`.resume` walks code channels and Conductor threads alike; the parts that exist
only for a thread (its admission, its room reading, its button lookup) live here so
``resume`` stays under the file-line cap. ``resume`` keeps its names for them.
"""

from __future__ import annotations

import asyncio
from typing import Any

from kiro_crew.slack.code_channels import binding as _binding
from kiro_crew.slack.code_channels import surface, thread_runtime
from kiro_crew.slack.code_channels.binding import thread_conductor_on
from kiro_crew.slack.code_channels.store import Binding


def admitted_now(state: Any, room: str, conductor: Any, *, caller: str) -> str:
    """:func:`.resume._admitted_now` for a Conductor thread (*room* is its ``channel:anchor``).

    The thread switch, the stored thread still this session's and neither ended
    nor wrapped up, this process's Stop, the workspace, the per-channel policy
    ceiling, the channel's activation, the room (private, or public by opt-in,
    never shared out) and the conductor's strict identity. No disk access.
    """
    from kiro_crew.slack.code_channels import resume
    from kiro_crew.slack.code_channels.routing import channel_and_origin_permit

    cfg = _binding.live_config()
    if not thread_conductor_on(cfg):
        return "Conductor threads are off"
    binding = thread_runtime.binding_for_room(room)
    if binding is None or thread_runtime.live_thread_binding(binding) is None:
        return "no longer a Conductor thread"
    if binding.wrapped_up_at:
        return "wrapped up"
    channel = binding.channel_id
    if surface.channel_stopped(channel):
        return "archived or stopped"
    if not surface.workspace_admitted(binding.team_id):
        return "workspace not admitted"
    if surface.channel_policy_refusal(
        channel, operation=resume.OPERATION, caller=caller, audit=False
    ):
        return resume.CHANNEL_POLICY_REFUSAL
    if not channel_and_origin_permit(cfg, channel, channel):
        return "channel or origin does not admit the bot"
    if why := thread_runtime.room_refusal(binding):
        return why
    if not resume._is_the_channels_session(state, binding, conductor):
        return "not the thread's session"
    return ""


async def prime_room(room: str) -> None:
    """:func:`.thread_runtime.prime_room` for the thread *room* names (a no-op for a channel)."""
    binding = await asyncio.to_thread(thread_runtime.binding_for_room, room)
    if binding is not None:
        await thread_runtime.prime_room(binding)


async def archive_refusal(binding: Binding) -> str:
    """:func:`.resume._archive_refusal` for a Conductor thread: ``""`` only when Slack says its channel is live.

    A DM is never archived. Otherwise the channel's archived state is read now
    (:func:`prime_room`) and an unreadable answer refuses, as for a code channel.
    """
    from kiro_crew.slack.code_channels import audience, resume
    from kiro_crew.slack.code_channels.store import ROOM_IM

    if binding.room == ROOM_IM:
        return ""
    await prime_room(thread_runtime.room_id(binding))
    archived = audience.channel_archived(binding.channel_id)
    if archived is None:
        return resume.ARCHIVE_UNREADABLE
    return "archived" if archived else ""


def room_unknown(why: str) -> bool:
    """Whether *why* is a Conductor thread's room reading being unknown, not a refusal."""
    from kiro_crew.dashboard.session_control_parts.code_channel_exemption import (
        AUDIENCE_UNREAD_REFUSAL,
        THREAD_PRIVACY_UNREAD_REFUSAL,
    )

    return why in (AUDIENCE_UNREAD_REFUSAL, THREAD_PRIVACY_UNREAD_REFUSAL)


async def binding_of_click(value: Any, channel: str) -> Binding | None:
    """The binding a notice button's value names, when it names one in *channel*.

    The value's ``channel`` is the room id: the channel itself for a code
    channel, ``channel:anchor`` for a Conductor thread. Either must live in the
    channel the press came from; anything else (a forged value, another
    channel's button) names nothing.
    """
    room = value.get("channel") if isinstance(value, dict) else None
    if not channel or not isinstance(room, str) or thread_runtime.channel_of_room(room) != channel:
        return None
    return await asyncio.to_thread(thread_runtime.binding_for_room, room)
