"""``!channel agent <Bot>|off`` from a shared user (``slack.shared_access``).

``!channel`` is the owner's. A confirmed shared user gets one part of it: putting
the channel they typed in on a Crew Bot, or taking it off one. The names are the
ones that person may already pick for a thread with ``!bot``: a ``config.agents``
key that starts a session. A plain kiro agent stays the owner's to pick.

What a shared user may change is an agent a shared user set, or none. The channel's
``shared_agent`` key is the record of that: it holds the Bot a shared user set, is
saved in the same locked write as ``agent`` (:func:`save`), and counts only while
the two are equal. The owner's ``!channel agent``, and any other change of
``agent``, makes the agent the owner's again
(``handler_runtime/inbound._persist_channel_config``,
``config.sections_slack.drop_stale_shared_agents``). Gateway start and channel
reload retire stale marks from the file with :func:`retire_stale_marks`.

A channel with an agent is served only by that agent, so a dashboard chat linked to
one of its threads stops getting that thread's messages when the agent is not its
own. A shared user's command never does that: with such a chat open it is refused,
before the write and again once the write is in force, when it is taken back.
A DM with the bot and a code channel are refused too; both keep the owner's own
controls.

Only the channel's ``agent`` and ``shared_agent`` keys are written: its activation,
approval and Trust settings are not touched. Every change and refusal is audited
with the sender's id.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: The command router sends only ``!channel agent ...`` here and answers the other
#: words itself, with this same reply.
OTHER_WORD_TEXT = "⛔ Owner-only command."
USAGE_TEXT = "Usage: `!channel agent <Bot>` or `!channel agent off`"
DM_TEXT = "⛔ Only the bot owner can set an agent in a DM."
CODE_CHANNEL_TEXT = "⛔ Only the bot owner can set a code channel's agent."
OWNER_AGENT_TEXT = "⛔ The bot owner set this channel's agent. Only they can change it."
LINKED_CHAT_TEXT = (
    "⛔ A dashboard chat is linked to a thread in this channel, and a new agent "
    "could cut it off. Only the bot owner can change the agent here."
)
CHANNEL_HANDLER_TEXT = (
    "⛔ The bot owner gave this channel one Bot or Conductor for every thread. "
    "Only they can change who answers here."
)
NO_AGENT_TEXT = "This channel has no agent set."
CLEARED_TEXT = "This channel is back on the default agent for new threads."


class ChannelAgentOwned(ValueError):
    """A shared user's write met a channel agent that is the owner's to change."""


def save(channel: str, agent: str, undo_of: str | None = None) -> str:
    """Save a shared user's *agent* for *channel* (``""`` takes it off), with its mark.

    One locked read-modify-write of ``config.json``, so the rule is asked on the file
    as it is then: a channel whose agent does not carry the mark is the owner's, and
    :class:`ChannelAgentOwned` is raised with nothing saved. The mark is compared
    with the agent as written, the way the loader compares them. Return the marked
    agent replaced under the lock, or ``""`` if none was replaced.

    *undo_of* makes this the taking back of the shared write that saved that Bot:
    it is saved only while the channel still holds that Bot with its mark, and
    otherwise nothing is saved and nothing is raised, so a write that landed in
    between (the owner's clear included) stays. Blocking: run it through
    ``run_config_write``.
    """
    from kiro_crew.config.loader import ConfigReadError, config_path, update_config_locked
    from kiro_crew.slack import handler

    path = config_path()
    if handler.is_sensitive_path(str(path)):
        raise ValueError("config.json is at a sensitive path")
    refusal = ""
    replaced = ""

    def _apply(data: dict) -> dict | None:
        nonlocal refusal, replaced
        entry: Any = data
        for name in ("slack", "channels", channel):
            entry = entry.setdefault(name, {})
            if not isinstance(entry, dict):
                refusal = "shape"
                return None
        current = entry.get("agent")
        # Anything but a missing or blank agent is one somebody wrote: a value that
        # is not a string is not this writer's to replace either.
        pinned = bool(current.strip()) if isinstance(current, str) else current is not None
        marked = pinned and entry.get("shared_agent") == current
        if undo_of is not None and not (marked and current == undo_of):
            return None
        if pinned and not marked:
            refusal = "owned"
            return None
        replaced = current if marked else ""
        entry["agent"] = agent
        if agent:
            entry["shared_agent"] = agent
        else:
            entry.pop("shared_agent", None)
        return data

    try:
        update_config_locked(path, mutate=_apply)
    except (ConfigReadError, OSError) as exc:
        raise ValueError(f"config.json could not be updated: {exc}") from exc
    if refusal == "owned":
        raise ChannelAgentOwned(f"the agent of channel {channel} is the owner's to change")
    if refusal:
        raise ValueError("slack.channels in config.json is not an object")
    return replaced


def retire_stale_marks() -> bool:
    """Delete stale shared-agent marks in one locked write; skip an unchanged file.

    Blocking: run through ``run_config_write``. Read or write failures leave the
    file alone and return False, as does a sensitive path.
    """
    from kiro_crew.config.loader import ConfigReadError, config_path, update_config_locked
    from kiro_crew.config.sections import drop_stale_shared_agents
    from kiro_crew.slack import handler

    changed = False

    def _apply(data: dict) -> dict | None:
        nonlocal changed
        changed = drop_stale_shared_agents(data)
        return data if changed else None

    try:
        path = config_path()
        if handler.is_sensitive_path(str(path)):
            logger.warning("slack shared-agent marks: config.json is at a sensitive path")
            return False
        update_config_locked(path, mutate=_apply)
    except (ConfigReadError, OSError, ValueError) as exc:
        # ValueError: a refused write, such as a hand edit still waiting for Apply.
        logger.warning("slack shared-agent marks could not be retired: %s", exc)
        return False
    return changed


async def retire_stale_marks_on_reload() -> None:
    """Retire stale marks off-loop under both config locks, without failing a reload."""
    from kiro_crew.slack import handler

    try:
        await handler.run_config_write(retire_stale_marks)
    except Exception:
        logger.warning("slack shared-agent mark retirement failed", exc_info=True)


def _audit(user: str, outcome: str, resources: str, error: str = "") -> None:
    from kiro_crew.slack import handler

    handler.sel().log_api_access(
        caller=user or "unknown",
        operation="slack.channel_agent",
        outcome=outcome,
        source="slack",
        resources=resources,
        error=error,
    )


def _is_code_channel(channel: str) -> bool:
    """Whether *channel* is a code channel; True when the store cannot say."""
    from kiro_crew.slack.code_channels.store import store

    try:
        codes = store()
        return (
            codes.get_binding(channel) is not None
            or codes.is_quarantined(channel)
            or codes.unreadable
        )
    except Exception:
        logger.debug("code channel lookup failed; failing closed", exc_info=True)
        return True


def _pin(handler: Any, channel: str) -> tuple[str, bool] | None:
    """*channel*'s agent and whether a shared user set it; ``None`` when unreadable.

    Read off the config every Slack turn reads, so an agent the owner keeps in
    ``config.local.json`` counts as the owner's.
    """
    try:
        cfg = handler.slack_cfg().channel_config(channel)
        agent = (cfg.agent or "").strip()
        return agent, bool(agent) and cfg.shared_agent == agent
    except Exception:
        logger.debug("channel agent lookup failed; failing closed", exc_info=True)
        return None


def _has_linked_chat(handler: Any, channel: str) -> bool:
    """Whether a dashboard chat owns a thread in *channel*, its tab open or closed.

    An open chat is read the way an inbound reply finds it, through the thread
    index (``get_linked_slot``): that index holds only a chat that mirrors out to a
    thread. A Slack thread's own tab carries the same link fields for the thread it
    lives in and is not in it, so it does not count. A closed chat is read off the
    session map: a reply in its thread still runs on that chat's session, and is
    refused once the channel's agent is another one
    (``closed_dashboard_owner_refused``).

    shortcut: any such chat refuses, one already on the Bot included; compare the
    chat's agent (``channel_agent_mismatch``) once shared users meet that refusal.
    """
    state = handler.get_dashboard_state()
    if state is None:
        return False
    try:
        for thread_ts in list(getattr(state, "_slack_to_slot", {})):
            slot = state.get_linked_slot(thread_ts)
            if (
                slot is not None
                and getattr(slot, "_slack_channel", "") == channel
                and not getattr(slot, "is_closing", False)
            ):
                return True
        sessions = getattr(state, "sessions", None)
        if sessions is None:
            return False
        if any(key.startswith("dashboard:") for key in sessions.slack_thread_owners(channel)):
            return True
        return False
    except Exception:
        logger.debug("linked chat lookup failed; failing closed", exc_info=True)
        return True


async def _take_back(handler: Any, channel: str, agent: str, written: str) -> None:
    """Put *channel* back on *agent*, the one a shared user's refused write replaced.

    Saved only while the channel still holds *written*, that refused write: a write
    that landed since, the owner's set or clear included, is left as it is.
    """
    try:
        await handler.run_config_write(save, channel, agent, undo_of=written)
    except ValueError:
        logger.warning("slack !channel agent: refused write not taken back", exc_info=True)
    handler._reload_orch_cfg()


async def handle(cmd_text: str, channel: str, user: str) -> str:
    """``!channel ...`` from a confirmed shared user; the reply to post.

    Run under the channel's handler lock, which the owner's ``!channel agent``,
    ``!channel bot`` and ``!channel conductor`` hold through their own check and
    write: a channel gets an agent, a Channel Bot or a Channel Conductor, never two.
    """
    from kiro_crew.slack import channel_handler

    async with channel_handler.lock(channel):
        return await _handle(cmd_text, channel, user)


async def _handle(cmd_text: str, channel: str, user: str) -> str:
    from kiro_crew.slack import channel_handler, handler

    parts = cmd_text.split(maxsplit=2)
    if len(parts) < 2 or parts[1].lower() != "agent":
        _audit(user, "denied", channel, "owner-only !channel word")
        return OTHER_WORD_TEXT
    typed = parts[2].strip() if len(parts) > 2 else ""
    if not typed:
        return USAGE_TEXT
    if not channel or channel == "dm" or channel.startswith("D"):
        _audit(user, "denied", channel, "dm")
        return DM_TEXT
    if await asyncio.to_thread(_is_code_channel, channel):
        _audit(user, "denied", channel, "code channel")
        return CODE_CHANNEL_TEXT
    # The owner's Channel Bot or Channel Conductor answers every thread here
    # (``slack/channel_handler.py``), so the channel takes no agent beside it and
    # has none to clear. The store read above loaded what this lookup reads.
    if channel_handler.handler_refusal(channel):
        _audit(user, "denied", channel, "channel bot or conductor")
        return CHANNEL_HANDLER_TEXT
    pin = _pin(handler, channel)
    if pin is None or (pin[0] and not pin[1]):
        _audit(user, "denied", channel, "owner's agent")
        return OWNER_AGENT_TEXT
    key = ""
    if typed.lower() != "off":
        # The names ``!bot`` takes, by the same two lookups.
        found = await asyncio.to_thread(handler.resolve_bot_name, typed)
        if found is None or await asyncio.to_thread(handler.resolve_channel_crew, found) is None:
            _audit(user, "denied", channel, "not a Crew Bot")
            listed = await asyncio.to_thread(handler.bots_text)
            return (
                f"❌ Unknown Bot `{typed[:80]}`. Crew Bots: {listed}. "
                "Only the bot owner can pick another kind of agent."
            )
        key = found
        # Taking the agent off cuts nothing off: a channel with none serves every chat.
        if _has_linked_chat(handler, channel):
            _audit(user, "denied", f"{channel}:{key}", "linked dashboard chat")
            return LINKED_CHAT_TEXT
    elif not pin[0]:
        _audit(user, "allowed", f"{channel}:default", "no agent to clear")
        return NO_AGENT_TEXT
    try:
        replaced = await handler.run_config_write(save, channel, key)
    except ChannelAgentOwned:
        _audit(user, "denied", channel, "owner's agent")
        return OWNER_AGENT_TEXT
    except ValueError:
        # The reason names the config file's path, which is the owner's to read.
        logger.warning("slack !channel agent: shared write refused", exc_info=True)
        _audit(user, "error", channel, "config write refused")
        return "❌ Could not save this channel's agent. Ask the bot owner."
    handler._reload_orch_cfg()
    # Asked again now that every link writer reads the new agent, with no await since
    # the config was loaded and applied: a chat linked while the write waited is seen
    # here, and one linked later is judged against the new agent by its own writer.
    in_force = _pin(handler, channel)
    refusal = ""
    if in_force is None or in_force[0] != key or (key and not in_force[1]):
        # The owner's agent in ``config.local.json``, or the owner's write just
        # after, the same Bot included: the agent in force is then the owner's.
        refusal, reply = "owner's agent", OWNER_AGENT_TEXT
    elif key and _has_linked_chat(handler, channel):
        refusal, reply = "linked dashboard chat", LINKED_CHAT_TEXT
    if refusal:
        if key:
            # A clear is left as saved: an empty agent cannot be told from the
            # owner's own clear, and the agent in force is the owner's either way.
            await _take_back(handler, channel, replaced, key)
        _audit(user, "denied", f"{channel}:{key or 'default'}", refusal)
        return reply
    _audit(user, "allowed", f"{channel}:{key or 'default'}")
    if not key:
        return CLEARED_TEXT
    return (
        f"Agent for this channel: *{key}*. Bot *{key}* now answers new threads here, "
        "for everyone. `!channel agent off` takes it back."
    )
