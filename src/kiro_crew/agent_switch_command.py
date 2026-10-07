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

import re

from kiro_crew.validation import is_registered_agent_name

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
