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
from typing import Any, Generic, Literal, TypeVar

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


# ── /agent on the channels (Webex, Discord, Teams, Feishu, Telegram) ──
#
# The text channels have no picker UI, so the command is text: a bare ``/agent``
# shows the current agent and the ones this machine offers, ``/agent <name>``
# switches, ``/agent default`` goes back to the configured default. Telegram
# keeps its button picker and its own replies, but a press switches through the
# same :func:`switch_channel_agent` step. Slack keeps its thread agent and
# reads none of this.

#: Words that are not agent names on the channel command.
_SHOW_WORDS = frozenset({"list"})
_RESET_WORDS = frozenset({"default"})

RouteT = TypeVar("RouteT", bound=Hashable)


class ChannelAgentPicks(Generic[RouteT]):
    """The agent each channel conversation picked with ``/agent``, by route.

    Keyed by ROUTE (a DM, a space, a thread) so the pick survives ``/new`` and
    the idle/daily rotation, on every channel including Telegram's button
    picker. In memory only: the durable answer is the configured default agent.
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


class LiveDefaultAgent:
    """``agent.default_agent`` as the config in force NOW names it, for a channel.

    The field applies live (it carries no ``restart`` mark, and Slack and the
    dashboard follow it), so a channel dispatcher reads it at the point of use
    instead of from the config it booted with. The agent is part of the session
    key, so a conversation that follows the default moves to the new agent's
    bucket; the dispatcher's ``ConversationState`` reseeds it there through its
    ``bucket_fn``. Read through ``default_template``, so a default whose spec was
    removed runs the managed template, as a new dashboard chat does.
    """

    def __init__(self) -> None:
        self._last: str | None = None

    def read(self, boot_cfg: Any, live_cfg: Any) -> str:
        """The default agent *live_cfg* names, or the one last in force.

        A DISCARDED ``agent`` section holds defaults, and reading those would
        drop the configured agent, so the value read last stays (the boot copy's
        on a first read), as Slack's default-agent applier keeps its own.
        """
        # Deferred, so importing this module does not load the config package.
        from kiro_crew.config.loader import default_template

        if "agent" in getattr(live_cfg, "degraded_sections", ()):
            if self._last is None:
                self._last = default_template(boot_cfg)
            return self._last
        self._last = default_template(live_cfg)
        return self._last

    def for_route(self, conv: Any, route: Any, boot_cfg: Any, live_cfg: Callable[[], Any]) -> str:
        """The default *route*'s current conversation runs, pinned in *conv*.

        The first read of a conversation pins the live default, and it stays until
        the conversation rotates (``/new``, an idle or daily reset, an ``/agent``
        switch). So a turn that is queued, admitted or running keeps the key it
        started under, and ``/stop`` and the busy check still find it. ``None``
        (no conversation named) reads the live default.
        """
        if route is None:
            return self.read(boot_cfg, live_cfg())
        return str(conv.pinned(route, lambda: self.read(boot_cfg, live_cfg())))


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


#: What :func:`switch_channel_agent` did: refused because a reply runs, set the
#: pick with no conversation live, or set it and left the live conversation.
SwitchOutcome = Literal["busy", "fresh", "left"]


def switch_channel_agent(
    picks: ChannelAgentPicks[RouteT],
    route: RouteT,
    target: str,
    *,
    conv: Any,
    sessions: Any,
    session_key: Callable[[], str],
) -> SwitchOutcome:
    """Move *route* to *target* (``""`` for the configured default), the step
    every channel's ``/agent`` switch shares; the caller words the reply.

    *session_key* derives the route's CURRENT key, read BEFORE the pick moves
    it: the agent is part of the key. A switch is refused while a reply runs on
    that key (``"busy"``), because everything about a running turn is keyed on
    it and moving the key would leave that turn with no route back. Otherwise
    the pick is set and *conv* (the channel's ``ConversationState``) is reseeded
    from the new agent's own bucket, so the route resumes that agent's latest
    conversation and ``/new`` goes past every one of its saved generations;
    switching back reaches the old agent's latest one again. ``"left"`` says a
    conversation was live and is being left, ``"fresh"`` that none was.
    """
    current_key = session_key()
    if sessions.is_busy(current_key):
        return "busy"
    had = sessions.has_session(current_key)
    picks.set(route, target)
    conv.reseed(route)
    return "left" if had else "fresh"


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
    running_default: str | None = None,
) -> str:
    """Answer one channel ``/agent`` command and return the reply text.

    *configured* is the agent a NEW conversation runs with no pick (the live
    default), which a reset switches to. *running_default* is the default the
    route's current conversation pinned, ``None`` when it is *configured*. *session_key*
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
    running = running_default or configured
    # *current* and *running* are read before the discovery await below; the
    # key taken with them tells whether a /new or a turn moved the route since.
    key_before = session_key()
    current_label = current or f"default ({running})"
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
    if (target or configured) == (current or running) and session_key() == key_before:
        # A reset still drops the pick, with no reseed: a pick equal to the
        # default must not stay pinned once the default moves on.
        # The default that agent equals is the live one, so it becomes the pin.
        if not target:
            picks.set(route, "")
            conv.repin(route, configured)
        elif target != configured:
            # Named explicitly while only the pinned default runs it: keep it past
            # the next rotation. The key is unchanged, so no reseed.
            picks.set(route, target)
        return f"ℹ️ Already using `{label}`."
    outcome = switch_channel_agent(
        picks, route, target, conv=conv, sessions=sessions, session_key=session_key
    )
    if outcome == "busy":
        again = f"{prefix}agent {target or 'default'}"
        return (
            f"⏳ Still working on your last message. Send `{again}` again "
            f"once it finishes to switch to `{label}`."
        )
    if outcome == "fresh":
        return f"✅ Agent set to `{label}`."
    return (
        f"✅ Agent set to `{label}`. This leaves the current conversation; "
        f"switch back to return to it."
    )
