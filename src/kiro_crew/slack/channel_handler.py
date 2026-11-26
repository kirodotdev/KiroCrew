"""One Bot or one Conductor answers a whole Slack channel.

``!channel bot <name>`` and ``!channel conductor on`` give an ordinary channel ONE
dashboard chat that answers every thread in it, and each reply is posted in the
thread it answers:

* **Channel Bot** (``slack.channels.<id>.bot``, a ``config.agents`` key): the chat is
  the Bot's own chat (its member thread on the Bots page), so every word a channel
  says to the Bot shows there. Only an OPEN Bot chat answers, as for Discord's
  ``!bot``: the Bots page is the one route that opens a closed one.
* **Channel Conductor**: a Conductor-thread binding marked ``channel_wide``
  (:mod:`kiro_crew.slack.conductor_thread` opens it), so every Conductor-thread rule
  holds for it as it is: only the owner's messages reach it, it needs
  ``slack.thread_conductor``, and a public channel needs
  ``slack.thread_conductor_public_channels``, both at the start and at each dispatch.
  ``!channel conductor off`` removes the binding, so no thread of the channel keeps it.

**How one chat answers many threads.** A dashboard chat is linked to ONE Slack
thread at a time, and a turn posts to the thread its chat was linked to when it
started. So before a message is routed, :func:`claim` points the chat's link (and a
Conductor's binding, through ``move_thread_binding``) at the message's thread, and
the usual linked-thread route runs it (or, in the thread the chat is already
answering, steers or queues it). While the chat is busy with another
thread a message from a new thread is told to wait instead, so the reply to a
message lands in that message's thread. Busy means a turn running or starting or
messages queued, in the chat itself or in a session it created, or sub-agents
attached to it (running, queued or still delivering their result). A thread that
already has its own route keeps it: a Conductor thread, or a link to another chat.

A turn the chat starts with no Slack message behind it (a worker's report that
arrives after the worker went idle, a patrol, a check-in, a message typed on the
dashboard) answers no thread: it posts in the thread the chat answered last.

Owner-only to set (``!channel`` is owner-only). DMs never have a channel handler.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from kiro_crew.slack.bot_chat import bot_chat_slot

logger = logging.getLogger(__name__)

CHANNEL_USAGE = (
    "Usage: `!channel always|mention|observe|off`, `!channel agent <name|off>`, "
    "`!channel bot <name|off>`, `!channel conductor on|off`"
)

#: The SEL operation each claim refusal is audited under.
OPERATION = "slack.channel_handler"

BUSY_TEXT = (
    "⏳ {name} is answering another thread in this channel. Send this again when it is done."
)
BOT_CLOSED_TEXT = (
    "Bot *{bot}* answers this channel, but its chat is not open. "
    "Open it on the Bots page, then send this again."
)
CONDUCTOR_CLOSED_TEXT = (
    "This channel's Conductor chat is closed. "
    "Send `!channel conductor off`, then `!channel conductor on` to start a new one."
)
HELD_TEXT = (
    "{name}'s chat is linked to {where}, so it cannot answer here. "
    "Unlink it on the dashboard first."
)
OWNER_ONLY_TEXT = "This channel has a Conductor; only the owner's messages reach it."
NOT_SAVED_TEXT = "{name} could not move to this thread. Try again."
REVIEW_TEXT = (
    "This channel is in review mode, so its Bot or Conductor does not answer here. "
    "Set `!channel mention` or `!channel always` first."
)
AGENT_FIRST_TEXT = "This channel runs agent *{agent}*. Send `!channel agent off` first."
REPLACED_TEXT = " It replaces agent *{agent}*, which a shared user set."
#: Appended when ``!channel agent`` saves a Bot key: name the Channel Bot path instead.
AGENT_BOT_HINT_TEXT = (
    "\nEach thread runs as this Bot on its own session. To have the Bot's own chat answer "
    "the whole channel, so it shows on the Bots page, send `!channel agent off` and then "
    "`!channel bot {bot}`."
)
PRIVATE_TEXT = (
    "This thread is answered by the channel's Bot or Conductor chat, which is saved, so "
    "`!temporary` and `!incognito` cannot apply here. Nothing was run. "
    "Send that message in a new thread."
)
#: The activation that holds every reply as a draft for the owner.
REVIEW = "review"
#: :func:`_changed`'s answer when another chat took the thread during a claim.
TAKEN = "taken"

#: One lock per channel: a claim and the route it hands the thread to run as one step.
_locks: dict[str, asyncio.Lock] = {}

#: The most threads remembered per channel in :data:`_answered`; the oldest goes first.
_ANSWERED_CAP = 1000
#: The threads each channel's handler chat has answered, oldest first, so thread-follow
#: still admits an unmentioned reply in one after the chat's link has moved on.
# shortcut: memory-only, so after a gateway restart only the thread the chat is linked
# to now counts as followed; persist it with the session map if that is not enough.
_answered: dict[str, dict[str, None]] = {}


def _handler() -> Any:
    from kiro_crew.slack import handler

    return handler


def _audit(user: str, outcome: str, resources: str, error: str = "") -> None:
    _handler().sel().log_api_access(
        caller=user or "unknown",
        operation=OPERATION,
        outcome=outcome,
        source="slack",
        resources=resources,
        error=error,
    )


async def _say(slack: Any, channel: str, text: str, thread_ts: str) -> None:
    """Post one of this module's own command replies (never agent text) in *thread_ts*."""
    await slack.post_message(channel, text, thread_ts)


def channel_bot(channel: str) -> str:
    """The Bot key set for *channel* (``slack.channels.<id>.bot``), ``""`` for none.

    From the live config, no disk access. DMs have none. An unreadable config
    reads as none, so the channel keeps its usual routing.
    """
    if not channel or channel.startswith("D"):
        return ""
    try:
        return str(_handler().slack_cfg().channel_config(channel).bot or "").strip()
    except Exception:
        logger.debug("channel bot lookup failed", exc_info=True)
        return ""


def channel_conductor(channel: str) -> Any | None:
    """The live Channel Conductor binding of *channel*, or ``None``.

    From the store's loaded snapshot only (no disk access); an unloaded store
    answers ``None``.
    """
    if not channel or channel.startswith("D"):
        return None
    try:
        from kiro_crew.slack.code_channels.store import store

        loaded = store().loaded_thread_bindings()
    except Exception:
        logger.debug("channel conductor lookup failed", exc_info=True)
        return None
    for binding in loaded or ():
        if binding.channel_wide and binding.channel_id == channel and not binding.ended_at:
            return binding
    return None


def _activation(channel: str) -> str:
    """*channel*'s live activation mode; an unreadable config reads as ``review``."""
    try:
        return str(_handler().slack_cfg().channel_config(channel).activation or "")
    except Exception:
        logger.debug("channel activation lookup failed", exc_info=True)
        return REVIEW


def _pinned(channel: str) -> tuple[str, str]:
    """*channel*'s agent, and that same agent again when a shared user set it.

    A shared user's agent (``slack/shared_channel_agent.py``, the ``shared_agent``
    mark) is replaced by the owner's later change, as the owner's ``!channel agent``
    replaces it: ``!channel bot <name>`` and ``!channel conductor on`` take such a
    channel. The owner's own agent has to be cleared first. An unreadable config
    reads as the owner's.
    """
    from kiro_crew.slack import shared_channel_agent

    handler = _handler()
    agent = handler.channel_pinned_agent(channel) or ""
    pin = shared_channel_agent._pin(handler, channel) if agent else None
    return agent, agent if pin is not None and pin == (agent, True) else ""


def status_line(channel: str) -> str:
    """Who answers *channel* now, in one line."""
    if channel_conductor(channel) is not None:
        return "This channel is answered by its Channel Conductor."
    bot = channel_bot(channel)
    if bot:
        return f"This channel is answered by Bot *{bot}*."
    try:
        agent = (_handler().slack_cfg().channel_config(channel).agent or "").strip()
    except Exception:
        agent = ""
    if agent:
        return f"This channel is answered by agent *{agent}*, one session per thread."
    return "This channel is answered by the default agent, one session per thread."


def handler_refusal(channel: str) -> str:
    """Why a thread here may not get its own Bot or Conductor; ``""`` when it may."""
    if channel_conductor(channel) is not None:
        return "This channel has a Channel Conductor. Send `!channel conductor off` first."
    bot = channel_bot(channel)
    if bot:
        return f"This channel is answered by Bot *{bot}*. Send `!channel bot off` first."
    return ""


def _turn_live(slot: Any) -> bool:
    """Whether *slot* has a turn running or starting, or messages queued."""
    task = getattr(slot, "task", None)
    starting = task is not None and not task.done()
    return bool(slot.running or starting or slot.queue_depth)


async def _busy(state: Any, slot: Any, key: str) -> bool:
    """Whether *slot* (session *key*) is answering a thread now.

    Busy with a turn of its own (:func:`_turn_live`), with a session it created
    that has one (``worker_approvals.live_workers``), or with sub-agents attached
    to it, running, queued or still delivering (``subagents_attached_async``,
    which fails closed). A worker's or sub-agent's result reaches the chat after
    its own turn ended, and its reply goes to the thread the chat is linked to
    then, so the link stays where that work was asked for.
    """
    # shortcut: a wake that is only scheduled (a worker report waiting to be delivered
    # to an idle chat) is not counted, so its turn posts in the thread answered last;
    # give each wake its origin thread if those must follow the asking thread.
    from kiro_crew.dashboard.chat_utils_completion import subagents_attached_async

    if _live(state, slot):
        return True
    return await subagents_attached_async(state, slot, key, OPERATION)


def _live(state: Any, slot: Any) -> bool:
    """Whether *slot*, or a session it created, has a turn running, starting or queued."""
    from kiro_crew.slack.code_channels.worker_approvals import live_workers

    return _turn_live(slot) or any(_turn_live(w) for w in live_workers(state, slot))


def _held_elsewhere(state: Any, key: str, channel: str) -> Any | None:
    """The link that ties session *key* to another channel or surface, or ``None``."""
    from kiro_crew.dashboard.session_control_parts.caller_access import _bare_channel

    held = state.sessions.get_mirror_link(key)
    if held is not None and (
        str(held.channel_type) != "slack" or _bare_channel(held.channel_id) != channel
    ):
        return held
    return None


def _changed(state: Any, slot: Any, key: str, channel: str, reply_ts: str) -> str:
    """What changed while ``claim`` awaited; ``""`` when the link may still move.

    Read with no await, directly before the link write. :data:`TAKEN`: another
    chat, open or closed, owns thread *reply_ts* now, or the thread turned private,
    so the thread keeps that route. Otherwise the notice for the sender: the handler chat started a turn
    (:data:`BUSY_TEXT`), or it was closed or linked elsewhere, which the sender's
    next message is told precisely (:data:`NOT_SAVED_TEXT`). A link the dashboard
    just gave the chat is left as it is: the same link made while nothing was
    being claimed is refused by ``claim`` the same way (:data:`HELD_TEXT`).
    """
    from kiro_crew.dashboard.chat_slack import slot_is_live

    owner = state.sessions.get_session_for_thread(reply_ts)
    if state.get_linked_slot(reply_ts) not in (None, slot) or (
        isinstance(owner, str) and owner.startswith("dashboard:") and owner != key
    ):
        return TAKEN
    # A ``!temporary`` or ``!incognito`` applied to the thread meanwhile: it keeps
    # its own session, as ``may_claim`` leaves a thread that was private before.
    if _private("", reply_ts):
        return TAKEN
    if not slot_is_live(state, slot) or _held_elsewhere(state, key, channel) is not None:
        return NOT_SAVED_TEXT
    return BUSY_TEXT if _live(state, slot) else ""


def _where(link: Any) -> str:
    from kiro_crew.dashboard.session_control_parts.caller_access import _bare_channel

    if str(link.channel_type) == "slack":
        return f"<#{_bare_channel(link.channel_id)}>"
    return str(link.channel_type).title()


def _remember(channel: str, thread_ts: str) -> None:
    """Record that *channel*'s handler chat answered *thread_ts*."""
    if not thread_ts:
        return
    threads = _answered.setdefault(channel, {})
    threads.pop(thread_ts, None)
    threads[thread_ts] = None
    if len(threads) > _ANSWERED_CAP:
        del threads[next(iter(threads))]


def answered(channel: str, thread_ts: str | None) -> bool:
    """Whether *channel* still has a handler and its chat has answered *thread_ts*.

    No disk access: the live config and the store's loaded snapshot.
    """
    if not thread_ts or thread_ts not in _answered.get(channel, {}):
        return False
    return bool(channel_bot(channel)) or channel_conductor(channel) is not None


def lock(channel: str) -> asyncio.Lock:
    """The lock a claim and its route hold for *channel*."""
    found = _locks.get(channel)
    if found is None:
        found = _locks[channel] = asyncio.Lock()
    return found


def handler_slot(state: Any, channel: str) -> Any | None:
    """The channel handler's open dashboard slot, or ``None``; no disk access."""
    if state is None:
        return None
    conductor = channel_conductor(channel)
    if conductor is not None:
        slot = state._slots.get(conductor.session_key)
        return slot if slot is not None and not getattr(slot, "is_closing", False) else None
    bot = channel_bot(channel)
    return bot_chat_slot(state, bot) if bot else None


async def may_claim(
    channel: str, text: str, *, commands: bool = True, session_key: str = "", reply_ts: str = ""
) -> bool:
    """Whether a message in *channel* goes to a channel handler at all.

    With *commands* enabled, bang commands and whole-message ``sessions`` keyword
    commands are not claimed, so their handlers remain reachable. A private thread
    is never claimed (:func:`_private`). The code-channel store is loaded off the
    loop first, once, so a Channel Conductor is found after a restart too.
    """
    if not channel or channel.startswith("D"):
        return False
    from kiro_crew.slack.code_channels.binding import load_store_for_links
    from kiro_crew.slack.code_channels.store import store

    if not store().loaded:
        await load_store_for_links(OPERATION)
    if channel_conductor(channel) is None and not channel_bot(channel):
        return False
    if _private(session_key, reply_ts) or (commands and _carries_modifier(text)):
        return False
    if not commands:
        return True
    word = _handler()._message_command_text(text).split(maxsplit=1)
    return not ((word and word[0].startswith("!")) or _handler()._is_sessions_keyword(text))


def _private(session_key: str, reply_ts: str) -> bool:
    """Whether thread *reply_ts* is ``!temporary`` or ``!incognito``.

    Such a thread stays off a saved dashboard chat, as it does for ``!bot``'s join
    (``_bang_bot``): it keeps its own session, where the mode holds. Asked of the
    routed key and of the thread's own Slack key. The callers restored the flags a
    restart dropped before they got here (``_hydrate_conv_flags``).
    """
    from kiro_crew.messaging.link import canonical_key

    keys = {session_key, canonical_key(reply_ts) if reply_ts else ""} - {""}
    return any(_handler()._is_slack_restricted(key) for key in keys)


def _carries_modifier(text: str) -> bool:
    """Whether a typed message carries ``!temporary`` or ``!incognito`` itself.

    The thread's own route applies the modifier only after the linked route is
    asked, so the message is kept off the handler chat here. A click's label is
    never read as one (the callers ask this with commands on only).
    """
    return bool(_handler().privacy_mode.strip_tokens(text)[1])


async def refuse_modifier(user_id: str, channel: str, text: str, slack: Any, reply_ts: str) -> bool:
    """Refuse a typed privacy modifier in the thread the handler chat answers now.

    That thread is linked to the saved chat, so the linked route would append the
    message there with no mode applied. True when the sender was told so; a new
    thread, or any thread the chat is not linked to, takes the modifier on its
    own session.
    """
    handler = _handler()
    state = handler._dashboard_state
    if state is None or not hasattr(state, "get_linked_slot") or not _carries_modifier(text):
        return False
    if not (handler.is_allowed_user(user_id) or handler.is_shared_user(user_id)):
        return False
    slot = handler_slot(state, channel)
    if slot is None or state.get_linked_slot(reply_ts) is not slot:
        return False
    await _say(slack, channel, PRIVATE_TEXT, reply_ts)
    return True


async def claim(user_id: str, channel: str, slack: Any, reply_ts: str) -> bool:
    """Point *channel*'s handler chat at thread *reply_ts*; True when refused here.

    False hands the message to the linked-thread route: the chat now answers this
    thread, or the thread keeps a route of its own. True means the sender was told
    why the message went no further. Call it holding :func:`lock`.
    """
    handler = _handler()
    state = handler._dashboard_state
    if state is None or not hasattr(state, "link_slack"):
        return False
    from kiro_crew.slack import conductor_thread

    conductor = channel_conductor(channel)
    bot = "" if conductor is not None else channel_bot(channel)
    if conductor is None and not bot:
        return False
    # Who may be heard at all, before anything moves: the linked route's own gate.
    if not (handler.is_allowed_user(user_id) or handler.is_shared_user(user_id)):
        return False
    own = conductor_thread.binding_for(channel, reply_ts)
    if own is not None and not (conductor is not None and own.anchor_ts == conductor.anchor_ts):
        return False  # a Conductor thread of its own
    slot = handler_slot(state, channel)
    linked = state.get_linked_slot(reply_ts)
    if linked is not None and linked is not slot:
        return False  # another chat owns this thread
    from kiro_crew.dashboard.chat_utils import effective_session_key

    owner = state.sessions.get_session_for_thread(reply_ts)
    if isinstance(owner, str) and owner.startswith("dashboard:"):
        key = (
            effective_session_key(slot)
            if slot is not None
            else (
                f"dashboard:{conductor.session_key}"
                if conductor is not None
                else await asyncio.to_thread(_closed_bot_session_key, bot)
            )
        )
        if owner != key:
            return False  # a closed dashboard chat still owns its thread
    if _activation(channel) == REVIEW:
        # Review mode holds every reply for the owner's approval; a dashboard chat
        # publishes its replies directly, so the handler chat is handed no thread of
        # this channel, the one it is linked to included. A thread with a route of
        # its own was left to that route above.
        await _say(slack, channel, REVIEW_TEXT, reply_ts)
        return True
    if linked is not None:
        return False  # the handler chat already answers this thread
    resources = f"channel={channel} thread={reply_ts}"
    if conductor is not None:
        name = "The Channel Conductor"
        if not handler.is_owner(user_id):
            _audit(user_id, "denied", resources, "channel conductor is owner-only")
            if conductor_thread._first_telling(channel, "*", user_id):
                try:
                    await slack.post_ephemeral(
                        channel, user_id, OWNER_ONLY_TEXT, thread_ts=reply_ts
                    )
                except Exception:
                    logger.debug("channel conductor owner-only notice failed", exc_info=True)
            return True
        if slot is None:
            await _say(slack, channel, CONDUCTOR_CLOSED_TEXT, reply_ts)
            return True
    else:
        name = f"Bot *{bot}*"
        if slot is None:
            await _say(slack, channel, BOT_CLOSED_TEXT.format(bot=bot), reply_ts)
            return True
    key = effective_session_key(slot)
    held = _held_elsewhere(state, key, channel)
    if held is not None:
        _audit(user_id, "denied", resources, "handler chat linked elsewhere")
        await _say(slack, channel, HELD_TEXT.format(name=name, where=_where(held)), reply_ts)
        return True
    if await _busy(state, slot, key):
        await _say(slack, channel, BUSY_TEXT.format(name=name), reply_ts)
        return True
    return not await _move(state, slot, key, conductor, channel, reply_ts, slack, name)


async def _move(
    state: Any,
    slot: Any,
    key: str,
    conductor: Any | None,
    channel: str,
    reply_ts: str,
    slack: Any,
    name: str,
) -> bool:
    """Move the handler chat's link (and a Conductor's binding) to *reply_ts*.

    True when the message may go on to the linked route: the link moved, or another
    chat took the thread meanwhile and keeps it. False when the sender was told why not.
    """
    from kiro_crew.dashboard.chat_slack import _drop_unsaved_link, slot_is_live
    from kiro_crew.dashboard.session_control_parts.caller_access import _bare_channel
    from kiro_crew.slack.code_channels.binding import AnchorHandoffRefused, check_anchor_handoff
    from kiro_crew.slack.code_channels.store import StoreWriteError, store

    previous, previous_channel = state.sessions.get_slack_link(key)
    paused = (
        state.sessions.is_slack_paused(key) if hasattr(state.sessions, "is_slack_paused") else False
    )
    if conductor is not None:
        try:
            await asyncio.to_thread(
                store().move_thread_binding, channel, conductor.anchor_ts, reply_ts
            )
        except StoreWriteError:
            logger.warning(
                "channel conductor %s: not moved to %s", channel, reply_ts, exc_info=True
            )
            await _say(slack, channel, NOT_SAVED_TEXT.format(name=name), reply_ts)
            return False
    # The last checks before the link moves, with no await between them and the
    # write. Nothing the dashboard does takes the channel lock, so what ``claim``
    # read may have changed during its awaits (the sub-agent probe in ``_busy``,
    # the binding save above): see ``_changed``.
    changed = _changed(state, slot, key, channel, reply_ts)
    if changed:
        undone = True
        if conductor is not None:
            try:
                await asyncio.to_thread(
                    store().move_thread_binding, channel, reply_ts, conductor.anchor_ts
                )
            except StoreWriteError:
                undone = False
                logger.warning("channel conductor %s: move not undone", channel, exc_info=True)
        if undone:
            if changed == TAKEN:
                return True
            await _say(slack, channel, changed.format(name=name), reply_ts)
            return False
        # Read again after the rollback's await: no await sits between this and the
        # link write below, so a private thread never reaches that write.
        if TAKEN in (changed, _changed(state, slot, key, channel, reply_ts)):
            # The thread is private or another chat's, and the binding could not go
            # back: the chat is unlinked instead of linked to it, so nothing of that
            # thread is saved here and no thread holds the link without the binding.
            # The next message in another thread moves both there.
            if previous:
                _drop_unsaved_link(state, slot, key, previous)
                state.push_slots_update()
            await _say(slack, channel, NOT_SAVED_TEXT.format(name=name), reply_ts)
            return False
        # The binding stayed on this thread, so the link follows it: a Conductor's
        # link without its binding would lose the owner-only gate.
    # ``link_slack``'s own body without its ``batched_save``, which would write the
    # session map inline on the loop: the thread has no other owner (``claim``
    # checked), so this is one debounced ``set_slack_link`` that ``aflush`` lands
    # off the loop (``create_bound_session`` links its anchor the same way).
    try:
        check_anchor_handoff(reply_ts, slot.key)
        state._link_slack_persisted(slot, slot.key, reply_ts, channel)
        # A rebind drops the mute; the owner muted the chat, not one thread.
        if paused:
            state.sessions.set_slack_paused(key, True)
        await state.sessions.aflush()
    except Exception as exc:
        logger.warning("channel handler %s: link to %s not saved", channel, reply_ts, exc_info=True)
        dropped = False
        if not isinstance(exc, AnchorHandoffRefused):
            dropped = state.sessions.get_slack_link(key)[0] == reply_ts
            _drop_unsaved_link(state, slot, key, reply_ts)
        restored_ok = True
        if conductor is not None:
            try:
                await asyncio.to_thread(
                    store().move_thread_binding, channel, reply_ts, conductor.anchor_ts
                )
            except StoreWriteError:
                restored_ok = False
                logger.warning("channel conductor %s: move not undone", channel, exc_info=True)
        # Restore only an unchanged, live slot whose previous thread is still free.
        # A Conductor link needs its binding back to retain the owner-only gate.
        if (
            dropped
            and restored_ok
            and previous
            and previous != reply_ts
            and _bare_channel(previous_channel or "") == channel
            and slot_is_live(state, slot)
            and not state.sessions.get_slack_link(key)[0]
            and state._slack_to_slot.get(previous) in (None, slot.key)
            and state.sessions.get_session_for_thread(previous) in (None, key)
        ):
            # No durability retry: the debounced save converges on the restored link.
            state._link_slack_persisted(slot, slot.key, previous, previous_channel)
            if paused:
                state.sessions.set_slack_paused(key, True)
            state.push_slots_update()
        text = (
            "This thread belongs to a code channel, so it cannot be answered here."
            if isinstance(exc, AnchorHandoffRefused)
            else NOT_SAVED_TEXT.format(name=name)
        )
        await _say(slack, channel, text, reply_ts)
        return False
    if previous and _bare_channel(previous_channel or "") == channel:
        _remember(channel, previous)
    _remember(channel, reply_ts)
    state.push_slots_update()
    return True


def _closed_bot_session_key(bot: str) -> str:
    """The persisted member chat's session key, without opening the chat. Blocking IO."""
    from kiro_crew import members
    from kiro_crew.dashboard.chat_utils import session_key_for

    slug = members.member_slug(bot)
    binding = members.read_dm_binding(slug)
    if binding is not None and binding["member"] != bot:
        raise ValueError("the Bot's chat belongs to another member")
    slot_key = binding["slot_key"] if binding is not None else members.member_slot_key(slug)
    return str(session_key_for(slot_key))


def _link_map(state: Any, sessions: Any) -> Any:
    """The session map an ``off`` unlinks in: the dashboard's, else the command's own.

    They are one object in a running gateway. With no dashboard state no chat is
    open, and the saved link is still cleared on *sessions*, so ``off`` never
    depends on the dashboard to clear what the channel was given.
    """
    links = state.sessions if state is not None else sessions
    if links is None:
        raise ValueError("no session map to unlink in")
    return links


async def release(channel: str, bot: str, sessions: Any = None) -> None:
    """Durably unlink *bot*'s open or closed chat from *channel*. Raises on a failed save."""
    state = _handler()._dashboard_state
    links = _link_map(state, sessions)
    from kiro_crew.dashboard.chat_slack import _drop_unsaved_link
    from kiro_crew.dashboard.chat_utils import effective_session_key
    from kiro_crew.dashboard.session_control_parts.caller_access import _bare_channel

    slot = bot_chat_slot(state, bot)
    key = (
        effective_session_key(slot)
        if slot is not None
        else await asyncio.to_thread(_closed_bot_session_key, bot)
    )
    thread_ts, linked_channel = links.get_slack_link(key)
    if thread_ts and _bare_channel(linked_channel or "") == channel:
        if slot is not None:
            _drop_unsaved_link(state, slot, key, thread_ts)
        else:
            links.clear_slack_link(key)
    # A retry must flush even when the failed write already cleared the in-memory link.
    await links.aflush()


async def _release_conductor(channel: str, binding: Any, sessions: Any = None) -> None:
    """Durably unlink the Channel Conductor's open or closed chat from its anchor thread.

    Only a link naming this channel and *binding*'s anchor is cleared, on both the
    ``dashboard:`` key and the bare key (the turn runner copies the bare key's link
    onto the prefixed one). Raises on a failed save.
    """
    state = _handler()._dashboard_state
    links = _link_map(state, sessions)
    from kiro_crew.dashboard.chat_slack import _drop_unsaved_link
    from kiro_crew.dashboard.session_control_parts.caller_access import _bare_channel

    anchor = binding.anchor_ts
    key = f"dashboard:{binding.session_key}"
    for each in (key, binding.session_key):
        thread_ts, linked_channel = links.get_slack_link(each)
        if thread_ts != anchor or _bare_channel(linked_channel or "") != channel:
            continue
        slot = state._slots.get(binding.session_key) if state is not None else None
        if slot is not None and each == key:
            _drop_unsaved_link(state, slot, key, anchor)
        else:
            links.clear_slack_link(each)
    # A retry must flush even when the failed write already cleared the in-memory link.
    await links.aflush()


# ── !channel bot / !channel conductor ─────────────────────────────────────


async def set_bot(
    cmd_text: str, slack: Any, channel: str, reply_ts: str, user_id: str, sessions: Any = None
) -> str:
    """``!channel bot <name|off>``: one Bot's own chat answers every thread here."""
    h = _handler()
    if channel.startswith("D"):
        await _say(slack, channel, "A DM cannot have a Channel Bot.", reply_ts)
        return ""
    rest = cmd_text.split(maxsplit=2)
    typed = rest[2].strip() if len(rest) > 2 else ""
    if not typed:
        await _say(
            slack,
            channel,
            f"Usage: `!channel bot <name>` or `!channel bot off`. "
            f"Crew Bots: {await asyncio.to_thread(h.bots_text)}",
            reply_ts,
        )
        return ""
    state = h._dashboard_state
    if typed.lower() == "off":
        key = ""
    elif state is None:
        # A Bot's chat is a dashboard chat, and ``off`` unlinks it through the same
        # state: what cannot be answered or cleared here is not set here.
        await _say(slack, channel, "❌ A Channel Bot needs the dashboard running.", reply_ts)
        return ""
    else:
        found = await asyncio.to_thread(h.resolve_bot_name, typed)
        if found is None or await asyncio.to_thread(h.resolve_channel_crew, found) is None:
            await _say(
                slack,
                channel,
                f"❌ Unknown Bot `{typed}`. Crew Bots: {await asyncio.to_thread(h.bots_text)}",
                reply_ts,
            )
            return ""
        key = found
    replaced = ""
    try:
        # The reserved key serializes Bot assignments across different channels.
        async with lock(channel), lock("!channel bot"):
            if not key:
                previous = channel_bot(channel)
                if previous:
                    try:
                        await release(channel, previous, sessions)
                    except Exception:
                        logger.warning("!channel bot off: unlink not saved", exc_info=True)
                        await _say(
                            slack,
                            channel,
                            "❌ Could not unlink this channel's Bot. Try `!channel bot off` again.",
                            reply_ts,
                        )
                        return ""
            else:
                if channel_conductor(channel) is not None:
                    await _say(
                        slack,
                        channel,
                        "This channel has a Channel Conductor. Send `!channel conductor off` first.",
                        reply_ts,
                    )
                    return ""
                agent, replaced = _pinned(channel)
                if agent and not replaced:
                    await _say(slack, channel, AGENT_FIRST_TEXT.format(agent=agent), reply_ts)
                    return ""
                current = channel_bot(channel)
                if current and current != key:
                    await _say(slack, channel, handler_refusal(channel), reply_ts)
                    return ""
                other = await asyncio.to_thread(_channel_with_bot, key, channel)
                if other:
                    await _say(
                        slack,
                        channel,
                        f"Bot *{key}* already answers <#{other}>. Send `!channel bot off` there first.",
                        reply_ts,
                    )
                    return ""
            # One write: the Bot goes on as the shared user's agent (and its mark) goes.
            cleared = {"agent": ""} if replaced else {}
            await h.run_config_write(h._persist_channel_config, channel, bot=key, **cleared)
            h._reload_orch_cfg()
            if not key:
                _answered.pop(channel, None)
    except ValueError as e:
        await _say(slack, channel, f"❌ {e}", reply_ts)
        return ""
    h.sel().log_api_access(
        caller=user_id,
        operation="slack.channel_bot",
        outcome="allowed",
        source="slack",
        resources=f"{channel}:{key or 'off'}",
    )
    if not key:
        await _say(
            slack,
            channel,
            "This channel has no Bot now. Each thread runs on its own again.",
            reply_ts,
        )
        return ""
    opened = state is not None and bot_chat_slot(state, key) is not None
    tail = "" if opened else " Open its chat on the Bots page so it can answer."
    if replaced:
        tail += REPLACED_TEXT.format(agent=replaced)
    await _say(
        slack,
        channel,
        f"🤖 Bot *{key}* now answers every thread in this channel, in its own chat. "
        f"`!channel bot off` to stop.{tail}",
        reply_ts,
    )
    return ""


async def set_agent(
    cmd_text: str, slack: Any, channel: str, reply_ts: str, session_key: str, user_id: str
) -> str:
    """``!channel agent <name|off>`` from the owner: each thread here runs as that agent.

    Refused beside a Channel Bot or Channel Conductor; ``off`` is always taken. The
    check and the write hold the channel lock, as :func:`set_bot` and
    :func:`set_conductor` do, so a channel never gets two of the three.
    """
    h = _handler()
    rest = cmd_text.split(maxsplit=2)
    if len(rest) < 3:
        await _say(
            slack, channel, "Usage: `!channel agent <name>` or `!channel agent off`", reply_ts
        )
        return ""
    # The name is the whole remainder, so a multi-word Bot display name stays whole.
    agent_name = rest[2].strip()
    async with lock(channel):
        if agent_name.lower() == "off":
            agent_name = ""
        elif handler_refusal(channel):
            await _say(slack, channel, handler_refusal(channel), reply_ts)
            return ""
        else:
            project = h._thread_projects.get(session_key)

            def _channel_agent(typed: str) -> str | None:
                # A Bot key runs the channel as that Bot (channel_crew), so an exact key
                # wins; a Bot named only by case or display name is matched last.
                if typed in h.crew_bots():
                    return str(typed)
                found = h._resolve_agent_name(typed, project) or h.resolve_bot_name(typed)
                return str(found) if found else None

            resolved = await asyncio.to_thread(_channel_agent, agent_name)
            if not resolved:
                names = await asyncio.to_thread(h._list_all_agent_names)
                bots = await asyncio.to_thread(h.bots_text)
                await _say(
                    slack,
                    channel,
                    f"Unknown agent `{agent_name}`. Available: {names}. Crew Bots: {bots}",
                    reply_ts,
                )
                return ""
            agent_name = resolved
        try:
            await h.run_config_write(h._persist_channel_config, channel, agent=agent_name)
            h._reload_orch_cfg()
        except ValueError as e:
            await _say(slack, channel, f"❌ {e}", reply_ts)
            return ""
    h.sel().log_api_access(
        caller=user_id,
        operation="slack.channel_agent",
        outcome="allowed",
        source="slack",
        resources=f"{channel}:{agent_name or 'default'}",
    )
    label = f"*{agent_name}*" if agent_name else "default"
    takes = bool(agent_name)
    if takes:
        takes = await asyncio.to_thread(_channel_bot_would_take, agent_name, channel)
    hint = AGENT_BOT_HINT_TEXT.format(bot=agent_name) if takes else ""
    await _say(slack, channel, f"Agent for this channel: {label}{hint}", reply_ts)
    return ""


def _channel_bot_would_take(bot: str, channel: str) -> bool:
    """True when *bot* is a Bot key that ``!channel bot`` could set here once the agent is off.

    Mirrors :func:`set_bot`'s refusals that hold beyond the pinned agent (a DM, no
    dashboard, a key with no crew record, a Bot another channel has), so the
    ``!channel agent`` hint never sends the owner into one. Blocking.
    """
    h = _handler()
    if channel.startswith("D") or h._dashboard_state is None or bot not in h.crew_bots():
        return False
    return h.resolve_channel_crew(bot) is not None and not _channel_with_bot(bot, channel)


def _channel_with_bot(bot: str, channel: str) -> str:
    """Another channel whose Channel Bot is *bot*, or ``""``. Blocking (a config read)."""
    from kiro_crew.config import KiroCrewConfig

    cfg = KiroCrewConfig.load()
    for other, ch_cfg in (getattr(cfg, "slack_channels", None) or {}).items():
        if other != channel and str(getattr(ch_cfg, "bot", "") or "") == bot:
            return str(other)
    return ""


async def set_conductor(
    parts: list[str],
    slack: Any,
    channel: str,
    reply_ts: str,
    msg_ts: str,
    session_key: str,
    user_id: str,
    sessions: Any = None,
) -> str:
    """``!channel conductor on|off``: one Conductor answers every thread here."""
    h = _handler()
    from kiro_crew.slack import conductor_thread

    action = parts[2].lower() if len(parts) > 2 else ""
    if action == "on":
        async with lock(channel):
            if channel_bot(channel):
                await _say(
                    slack,
                    channel,
                    "This channel is answered by a Bot. Send `!channel bot off` first.",
                    reply_ts,
                )
                return ""
            agent, replaced = _pinned(channel)
            if agent and not replaced:
                await _say(slack, channel, AGENT_FIRST_TEXT.format(agent=agent), reply_ts)
                return ""
            started, text = await conductor_thread.start(
                slack,
                channel,
                reply_ts,
                user_id,
                "",
                session_key=session_key,
                msg_ts=msg_ts,
                channel_wide=True,
            )
            if started and replaced:
                # Only once the Conductor is there: a refused start changes nothing.
                try:
                    await h.run_config_write(h._persist_channel_config, channel, agent="")
                    h._reload_orch_cfg()
                    await _say(
                        slack, channel, REPLACED_TEXT.format(agent=replaced).strip(), reply_ts
                    )
                except ValueError as e:
                    await _say(
                        slack,
                        channel,
                        f"❌ Agent *{replaced}* could not be taken off: {e} "
                        "Send `!channel agent off`.",
                        reply_ts,
                    )
        if not started:
            await _say(slack, channel, f"❌ {text}", reply_ts)
        return ""
    if action == "off":
        from kiro_crew.slack.code_channels.store import store

        # Under the claim lock, so no claim moves the binding between this read and the drop.
        async with lock(channel):
            binding = channel_conductor(channel)
            if binding is None:
                await _say(slack, channel, "This channel has no Conductor.", reply_ts)
                return ""
            # Unlinked first, so a failed save leaves the row live for the next claim
            # to relink; the row is then removed, leaving its thread an ordinary one.
            try:
                await _release_conductor(channel, binding, sessions)
            except Exception:
                logger.warning("!channel conductor off: unlink not saved", exc_info=True)
                await _say(
                    slack,
                    channel,
                    "❌ Could not unlink the Conductor's chat. "
                    "Try `!channel conductor off` again.",
                    reply_ts,
                )
                return ""
            try:
                dropped = await asyncio.to_thread(
                    store().drop_channel_conductor, channel, binding.anchor_ts
                )
            except Exception as e:
                logger.warning("!channel conductor off: not saved", exc_info=True)
                await _say(slack, channel, f"❌ Could not end the Conductor: {e}", reply_ts)
                return ""
        if dropped is None:
            await _say(slack, channel, "❌ Could not end the Conductor. Try again.", reply_ts)
            return ""
        _answered.pop(channel, None)
        h.sel().log_api_access(
            caller=user_id,
            operation="slack.channel_conductor",
            outcome="allowed",
            source="slack",
            resources=f"{channel}:off slot={binding.session_key}",
        )
        await _say(
            slack,
            channel,
            "This channel's Conductor has ended. Each thread runs on its own again; "
            "its chat stays on the dashboard.",
            reply_ts,
        )
        return ""
    await _say(
        slack, channel, "Usage: `!channel conductor on` or `!channel conductor off`", reply_ts
    )
    return ""
