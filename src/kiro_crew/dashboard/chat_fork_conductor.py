"""Where a person's fork of a conductor mode root lands: a normal chat.

A person's fork (``POST /api/chat/slots/{slot}/fork``) of a conductor mode root
runs the default template with conductor mode off. The child keeps the parent's
transcript, memory store and memory mode, as every fork does, and is an ordinary
top-level chat: no ``crew_root``, and no ledger or Board of its own. The person
may turn conductor mode on in it like in any chat with messages. The conductor
chat, its ledger, its Board and its workers are not touched, and the lock that
keeps a root on its conductor (``crew_mode_locked``) is never asked.

Every other fork keeps the parent's agent, and so does a root whose default
template is itself a conductor: there is no normal chat to land on.
"""

from __future__ import annotations

import asyncio
from typing import Any

from kiro_crew import crew_mode
from kiro_crew.config.loader import KiroCrewConfig, default_template, dispatch_kiro_agent


def _default_chat_template() -> str:
    """The template a new plain chat is stamped with, as slot create stamps it.

    Off the event loop: :func:`dispatch_kiro_agent` warms a cold snapshot only there.
    """
    return dispatch_kiro_agent(default_template(KiroCrewConfig.load()))


async def conductor_root_fork_template(slot: Any, execution: Any) -> str:
    """The template a person's fork of *slot* runs instead of its agent, or ``""``.

    *execution* is the parent's frozen execution. A root runs a conductor as a
    TEMPLATE, so its execution names no member; requiring that keeps a member's
    private store from being carried onto another agent.
    """
    if not crew_mode.slot_is_crew_mode_root(slot) or execution.member_id is not None:
        return ""
    template = await asyncio.to_thread(_default_chat_template)
    return "" if template in crew_mode.conductor_agents() else template
