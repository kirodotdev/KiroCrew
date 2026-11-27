"""``!bot <name>`` joins a Slack thread to the Bot's own dashboard chat.

Discord's ``!bot`` binds the conversation to the Bot's LIVE member thread
(``discord/session_resume.DiscordSessionResume._bot_choices``), so its turns land in
the chat the owner reads on the Bots page. Slack reaches the same place through the
link every dashboard-to-Slack thread already uses: ``DashboardState.link_slack``
points the thread at the slot, ``route_linked_thread`` runs or queues each message
on it (a running dashboard turn queues the Slack message behind it), and the slot's
replies are mirrored back into the thread. Nothing new is minted here.

Only a live Bot chat is joined, as on Discord: the Bots page is the one route that
opens a closed one. With none open, in a channel pinned to an agent (every thread
there is that agent's), or with no dashboard, ``!bot`` keeps its older behaviour and
runs the thread as the Bot on its own session.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


def bot_chat_slot(state: Any, bot: str) -> Any | None:
    """Bot *bot*'s own chat (its member thread) open on the dashboard, or ``None``."""
    from kiro_crew.dashboard.slot_ownership import is_member_thread

    for slot in list(getattr(state, "_slots", {}).values()):
        if is_member_thread(slot) and slot.agent == bot and not getattr(slot, "is_closing", False):
            return slot
    return None


def joined_bot_chat(state: Any, thread_ts: str) -> Any | None:
    """The Bot chat Slack thread *thread_ts* is joined to, or ``None``."""
    from kiro_crew.dashboard.slot_ownership import is_member_thread

    if state is None or not hasattr(state, "get_linked_slot"):
        return None
    slot = state.get_linked_slot(thread_ts)
    return slot if is_member_thread(slot) else None


def _thread_url(channel: str | None, thread_ts: str | None) -> str:
    return f"https://slack.com/archives/{channel}/p{(thread_ts or '').replace('.', '')}"


def _keeps_thread_path(channel: str) -> bool:
    """Whether *channel* never joins: pinned to an agent, or its replies wait for the owner.

    Every thread of a pinned channel is that agent's. A ``review`` channel (or DM) shows
    each reply to the owner as a draft first, which a chat's mirrored replies would skip.
    A config that cannot be read keeps the thread path too.
    """
    from kiro_crew.slack import handler

    if handler.channel_pinned_agent(channel) is not None:
        return True
    try:
        activation = handler.slack_cfg().channel_config(channel).activation
    except Exception:
        logger.debug("channel activation lookup failed; keeping the thread path", exc_info=True)
        return True
    return bool(activation == handler.ACTIVATION_REVIEW)


def bot_chat_target(state: Any, bot: str, channel: str, thread_ts: str) -> tuple[Any | None, str]:
    """The Bot chat ``!bot <bot>`` would join from this thread, checked but not linked.

    ``(slot, "")`` when the chat can take the thread, ``(None, refusal)`` when it may
    not, and ``(None, "")`` when there is no chat to join, so the caller keeps the older
    per-thread Bot session. A chat answers one conversation at a time (Discord's
    ``binding_conflict``): one already linked to another Slack thread, or bound to
    another channel, refuses.
    """
    if state is None or not hasattr(state, "link_slack"):
        return None, ""
    if _keeps_thread_path(channel):
        return None, ""
    slot = bot_chat_slot(state, bot)
    if slot is None:
        return None, ""
    from kiro_crew.dashboard.chat_utils import effective_session_key

    held = state.sessions.get_mirror_link(effective_session_key(slot))
    if held is not None and not (held.channel_type == "slack" and held.thread_id == thread_ts):
        if held.channel_type == "slack":
            where = f"<{_thread_url(held.channel_id, held.thread_id)}|another Slack thread>"
        else:
            where = str(held.channel_type).title()
        return None, (
            f"Bot *{bot}*'s chat is already talking to {where}. "
            "Send `!bot off` there, or unlink it on the dashboard, first."
        )
    return slot, ""


def audit(outcome: str, session_key: str, user_id: str, channel: str, **details: str) -> None:
    """One audit row for a ``!bot`` join, leave or refusal on *session_key*."""
    from kiro_crew.slack import handler

    handler.sel().log_tool_invocation(
        session_key=session_key,
        source="slack",
        tool_name="!bot",
        tool_kind="command",
        outcome=outcome,
        metadata={"user": user_id, "channel": channel, **details},
    )


async def switch_refusal(
    state: Any,
    key: str | None,
    typed: str,
    session_key: str,
    channel: str,
    thread_ts: str,
    user_id: str,
) -> str:
    """Why ``!bot <typed>`` (resolved to *key*) may not join a Bot's open chat, or ``""``.

    An unknown Bot, a thread whose memory belongs to another Bot (``memory_binding_refusal``,
    asked before a join as the per-thread path asks it in ``bind_bot_memory``), and a
    Bot chat that holds another conversation all refuse. The memory rule is asked here
    only for a join; the per-thread path asks it itself.
    Every rule is read off the handler module, the one place a test patches them.
    """
    from kiro_crew.slack import handler

    if key is None or await asyncio.to_thread(handler.resolve_channel_crew, key) is None:
        bots = await asyncio.to_thread(handler.bots_text)
        return f"❌ Unknown Bot `{typed}`. Crew Bots: {bots}"
    slot, refusal = bot_chat_target(state, key, channel, thread_ts)
    if refusal:
        return f"❌ {refusal}"
    if slot is not None:
        try:
            memory = await handler.memory_binding_refusal(session_key, key)
        except handler.UnknownMemoryStore as exc:
            memory = str(exc)
        if memory:
            audit(
                "agent_denied_memory_binding",
                session_key,
                user_id,
                channel,
                agent=key,
                reason=memory,
            )
            return f"❌ {memory}"
    return ""


async def join_bot_chat(
    state: Any, bot: str, channel: str, thread_ts: str
) -> tuple[Any | None, str]:
    """Link Slack thread *thread_ts* to Bot *bot*'s own chat; ``bot_chat_target``'s answer.

    The link is saved before the caller says the thread joined, as the dashboard's
    Slack link does (``api_chat_slot_slack_link``): a write that fails takes this
    slot's binding back down (``_drop_unsaved_link``) and refuses.
    """
    from kiro_crew.dashboard.chat_slack import _drop_unsaved_link, _slack_link_lock
    from kiro_crew.dashboard.chat_utils import effective_session_key
    from kiro_crew.slack.code_channels.binding import AnchorHandoffRefused, load_store_for_links

    # Loaded first, as every link to an existing thread does, so the anchor check in
    # ``link_slack`` reads the code channel store and not an empty one.
    if state is not None:
        await load_store_for_links("slack.bot_chat_join")
    slot, refusal = bot_chat_target(state, bot, channel, thread_ts)
    if slot is None:
        return None, refusal
    async with _slack_link_lock(state, effective_session_key(slot)):
        # A dashboard Connect may have linked the chat while we waited for its lock.
        # Re-check under the lock, with no await between this check and the link.
        slot, refusal = bot_chat_target(state, bot, channel, thread_ts)
        if slot is None:
            return None, refusal
        try:
            state.link_slack(slot.key, thread_ts, channel)
            await state.sessions.aflush()
        except AnchorHandoffRefused:
            return None, "This thread belongs to a code channel, so it cannot join a Bot's chat."
        except Exception:
            logger.warning("slack !bot: joining %s's chat was not saved", bot, exc_info=True)
            _drop_unsaved_link(state, slot, effective_session_key(slot), thread_ts)
            return None, f"Could not save joining Bot *{bot}*'s chat. Try again."
    from kiro_crew.slack import handler, thread_trust

    # The thread now runs on the Bot's chat: a shared user's own Trust on the
    # thread's own session does not resume when it leaves.
    thread_trust.end_sender_grants(handler.canonical_key(thread_ts), "joined_bot_chat")
    return slot, ""


def privacy_sources(state: Any, joined: Any, incoming_key: str) -> tuple[str, ...]:
    """Include the joined slot's own session even when native dispatch passes Slack's key."""
    if joined is None:
        return (incoming_key,)
    from kiro_crew.dashboard.chat_utils import effective_session_key
    from kiro_crew.slack import handler

    key = effective_session_key(joined)
    # The row is already loaded in memory; restore its flags after a gateway restart.
    handler._hydrate_conv_flags(state.sessions, key)
    return (key,) if key == incoming_key else (key, incoming_key)


async def preserve_privacy(
    incoming_keys: tuple[str, ...],
    session_key: str,
    user_id: str,
    channel: str,
    slack: Any,
    sessions: Any,
    thread_ts: str,
) -> bool:
    """Carry the joined chat's modes to the thread before unlinking; refuse closed."""
    from kiro_crew.slack import handler

    privacy = handler.privacy_mode
    try:
        for mode, active in (
            (privacy.MODE_TEMPORARY, any(privacy.is_temporary(key) for key in incoming_keys)),
            (privacy.MODE_INCOGNITO, any(privacy.is_incognito(key) for key in incoming_keys)),
        ):
            if active:
                # Persist and announce without taking ownership from the joined chat:
                # a later mode can still refuse, in which case its link must survive.
                await handler._apply_privacy_mode(
                    mode,
                    session_key,
                    user_id,
                    channel,
                    slack,
                    sessions,
                    thread_ts,
                    link_thread=False,
                )
    except privacy.PrivacyModeRefused:
        return False
    return True


async def leave_bot_chat(state: Any, thread_ts: str) -> Any | None:
    """Unlink Slack thread *thread_ts* from the Bot chat it joined; the slot, or ``None``.

    The same clear and teardown as the dashboard's Slack unlink
    (``chat_slack.clear_slot_slack_link`` + ``finish_slack_unlink``), so a later reply
    in the thread runs on its own Slack session again. A failed save raises, so the
    caller tells the thread it left only once the unlink is on disk.
    """
    slot = joined_bot_chat(state, thread_ts)
    if slot is None:
        return None
    from kiro_crew.dashboard.chat_slack import clear_slot_slack_link, finish_slack_unlink
    from kiro_crew.dashboard.chat_utils import effective_session_key

    key = effective_session_key(slot)
    clear_slot_slack_link(state, key)
    try:
        await finish_slack_unlink(state, slot, key, thread_ts)
    finally:
        state.push_slots_update()
    return slot
