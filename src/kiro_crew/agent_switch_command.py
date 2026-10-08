"""The ``/agent <name>`` command, as Crew reads it on every surface.

Crew performs the switch itself instead of forwarding the words to kiro-cli:
with the native skill projection on (the default), kiro-cli is never allowed
to change agents mid-session, and with it off the process would move to the
new agent without Crew's skill scope following. A dashboard chat switches
through ``switch_slot_agent`` (the agent picker's transaction); a Slack thread
not linked to a dashboard chat switches its thread agent.

Mirrored by the dashboard composer's ``agentSwitchTarget``
(``website/src/pages/chat/ChatInput.tsx``), which routes the main composer's
command to the same switch before the message is ever sent.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable, Hashable
from typing import Any, Generic, TypeVar

from kiro_crew.validation import is_registered_agent_name

logger = logging.getLogger(__name__)

# One token in the registered-agent grammar, nothing after it.
_AGENT_SWITCH_RE = re.compile(r"/agent\s+([A-Za-z0-9][A-Za-z0-9_.-]*)\s*\Z")

#: kiro-cli's own ``/agent`` subcommands. They are not agent names, so they keep
#: going to the harness unchanged (``list`` and ``schema`` answer there).
AGENT_SUBCOMMANDS = frozenset(
    {
        "list",
        "schema",
        "create",
        "edit",
        "generate",
        "validate",
        "migrate",
        "set-default",
        "swap",
        "delete",
        "help",
    }
)


def switch_announcement(agent: str, warning: str = "") -> str:
    """The transcript line a successful ``/agent <name>`` switch appends.

    One wording for the composer's switch (the route's ``announce`` flag) and
    the in-turn command, so the main chat and a split pane read the same.
    """
    text = f"🔄 Switched to agent: {agent}"
    if warning:
        text += f"\n\n⚠️ {warning}"
    return text


def agent_switch_target(message: str) -> str | None:
    """The agent a ``/agent <name>`` message switches to, or None when it is not one.

    None for bare ``/agent``, a subcommand, more than one word after it, a
    look-alike (``/agents x``) and a name outside the agent-name grammar.
    """
    match = _AGENT_SWITCH_RE.match(message.strip())
    if match is None:
        return None
    name = match.group(1)
    if name.lower() in AGENT_SUBCOMMANDS or not is_registered_agent_name(name):
        return None
    return name


# ── /agent on the text-command channels (Webex, Discord, Teams, Feishu) ──
#
# Those channels have no picker UI, so the command is text: a bare ``/agent``
# shows the current agent and the ones this machine offers, ``/agent <name>``
# switches, ``/agent default`` goes back to the configured default. Telegram
# keeps its button picker and Slack its thread agent; neither reads this.

#: Words that are not agent names on the channel command.
_SHOW_WORDS = frozenset({"list"})
_RESET_WORDS = frozenset({"default"})

RouteT = TypeVar("RouteT", bound=Hashable)


class ChannelAgentPicks(Generic[RouteT]):
    """The agent each channel conversation picked with ``/agent``, by route.

    Keyed by ROUTE (a DM, a space, a thread), like Telegram's ``_agent_pref``,
    so the pick survives ``/new`` and the idle/daily rotation. In memory only:
    the durable answer is the configured default agent.
    """

    def __init__(self) -> None:
        self._picks: dict[RouteT, str] = {}

    def get(self, route: RouteT) -> str:
        """The picked agent for *route*, or ``""`` when none was picked."""
        return self._picks.get(route, "")

    def resolve(self, route: RouteT | None, configured: str) -> str:
        """The agent *route* runs: its pick, else *configured*."""
        if route is None:
            return configured
        return self._picks.get(route) or configured

    def set(self, route: RouteT, agent: str) -> None:
        if agent:
            self._picks[route] = agent
        else:
            self._picks.pop(route, None)


def pickable_agent_names() -> list[str]:
    """The agents a channel ``/agent`` may switch to, sorted.

    Telegram's ``/agent`` picker list, read from its one helper: Kiro Crew's
    internal agents and app-installed agents are never offered, so a channel
    switch can only reach an agent a person would pick. Reads and parses spec
    files on a cache miss, so callers run it off the loop.
    """
    from kiro_crew.telegram.dispatch.pickers import _installed_agent_names

    return _installed_agent_names()


def channel_agent_usage(prefix: str = "/") -> str:
    """The one-line usage the channel command answers a bad argument with."""
    return (
        f"Send `{prefix}agent` to see the agents, `{prefix}agent <name>` to switch, "
        f"or `{prefix}agent default` to go back to the default."
    )


async def handle_channel_agent_command(
    picks: ChannelAgentPicks[RouteT],
    route: RouteT,
    arg: str,
    *,
    conv: Any,
    configured: str,
    sessions: Any,
    session_key: Callable[[], str],
    prefix: str = "/",
) -> str:
    """Answer one channel ``/agent`` command and return the reply text.

    *configured* is the agent a conversation runs with no pick. *session_key*
    derives the route's CURRENT key. The agent is part of the key, so a switch
    moves the route to the new agent's own bucket: *conv* (the channel's
    ``ConversationState``) is reseeded from that bucket, which resumes the new
    agent's latest conversation there and keeps ``/new`` past every one of its
    generations. Switching back reaches the old agent's latest one again.

    A switch is refused while a reply runs, and only an agent from
    :func:`pickable_agent_names` is accepted, so a typo or an internal agent
    name never becomes the next turn's agent.
    """
    word = arg.strip()
    current = picks.get(route)
    current_label = current or f"default ({configured})"
    if not word or word.lower() in _SHOW_WORDS:
        try:
            names = await asyncio.to_thread(pickable_agent_names)
        except Exception:
            logger.warning("channel /agent: agent discovery failed", exc_info=True)
            names = []
        lines = [f"**Agent**: currently `{current_label}`"]
        if names:
            lines += [
                "",
                *(f"- `{name}`{' (current)' if name == current else ''}" for name in names),
            ]
        else:
            lines += ["", "No other agents are installed on this machine."]
        lines += ["", channel_agent_usage(prefix)]
        return "\n".join(lines)

    if word.lower() in _RESET_WORDS:
        target = ""
    else:
        target = agent_switch_target(f"/agent {word}") or ""
        if not target:
            return f"❌ That is not an agent name. {channel_agent_usage(prefix)}"
        try:
            names = await asyncio.to_thread(pickable_agent_names)
        except Exception:
            logger.warning("channel /agent: agent discovery failed", exc_info=True)
            names = []
        if target not in names:
            return (
                f"❌ No agent named `{target}` is available here. "
                f"Send `{prefix}agent` for the list."
            )
    label = target or f"default ({configured})"
    # Compared as the agent each side RUNS: with no pick, naming the default
    # agent changes nothing, so it must not read as a switch.
    if (target or configured) == (current or configured):
        return f"ℹ️ Already using `{label}`."
    # Both read the key BEFORE the pick moves it: the agent is part of the key.
    current_key = session_key()
    if sessions.is_busy(current_key):
        again = f"{prefix}agent {target or 'default'}"
        return (
            f"⏳ Still working on your last message. Send `{again}` again "
            f"once it finishes to switch to `{label}`."
        )
    had = sessions.has_session(current_key)
    picks.set(route, target)
    conv.reseed(route)
    if not had:
        return f"✅ Agent set to `{label}`."
    return (
        f"✅ Agent set to `{label}`. This leaves the current conversation; "
        f"switch back to return to it."
    )
