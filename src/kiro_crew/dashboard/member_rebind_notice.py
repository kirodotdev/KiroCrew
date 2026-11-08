"""The one notice a Bot's chat gives when the Bot is set to another agent than the chat runs.

A ``member`` execution record keeps the template it names (crew-mode.md, "Any
other member keeps the template its record names"), so once a Bot's
``kiro_agent`` is rebound, a chat that already holds a record keeps running the
agent it started with while a new chat with the Bot runs the new one. Turn
admission hands each turn that reuses a record to :func:`surface_rebind_notice`,
which tells the user once per chat and per new agent:

* a ``notice`` row naming the Bot, the agent a new chat runs and the agent this
  chat keeps (``meta.kind`` :data:`MEMBER_REBIND_NOTICE_KIND`, localized by the
  dashboard from its ``gateway_notice`` token);
* in the Bot's own pinned thread, whose chat draws no ``notice`` row (the
  decision record "the crewmate chat draws messages only"), one hidden turn
  after the current one in which the Bot tells its user
  (``mate_thread_wakes.offer_rebind_notice``).

Both agents come from the turn's bindings, which read them in the thread hop
that resolves the record (``session_agent_selection``), so a turn whose chat
runs what a new chat would does no further work here. "Once" is a
compare-and-set of :data:`REBIND_NOTICE_FIELD` on the session's metadata line,
so a Reload, a resume or a restart does not repeat it, and a later rebind to yet
another agent is told again. No record is rewritten.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: ``meta.kind`` of the notice row.
MEMBER_REBIND_NOTICE_KIND = "member_rebind"
#: The metadata-line field naming the agent this chat was last told about.
REBIND_NOTICE_FIELD = "member_rebind_notice"


def rebound_agents(bindings: Any) -> tuple[str, str]:
    """``(agent, recorded)``: what a new chat with the Bot runs, and what this chat runs.

    ``("", "")`` unless *bindings* resolved a ``member`` record whose Bot a new
    chat would run on another agent than the record does. No I/O.
    """
    agent = str(getattr(bindings, "new_chat_agent", "") or "")
    recorded = str(getattr(bindings, "kiro_agent", "") or "")
    if not agent or not recorded or agent == recorded:
        return "", ""
    return agent, recorded


def bot_name(config: Any, execution: Any) -> str:
    """The Bot *execution* selects, by its display name, else its config key; ``""`` if none."""
    from kiro_crew.execution_context import member_config_for_id
    from kiro_crew.memory_stores import UnknownMemoryStore

    try:
        name, member_cfg = member_config_for_id(config, getattr(execution, "member_id", "") or "")
    except UnknownMemoryStore:
        return ""
    label = " ".join(str(getattr(member_cfg, "display_name", "") or "").split())
    return label or name


def claim_rebind_notice(conversation_log: Any, session_key: str, agent: str) -> bool:
    """Record that *session_key* is told about *agent*. True only for the first claim.

    A session with no transcript, or a line that cannot be written, claims
    nothing, so it is told nothing. Blocking file I/O.
    """
    return bool(
        conversation_log.update_metadata_if(
            session_key,
            {REBIND_NOTICE_FIELD: agent},
            lambda metadata: metadata.get(REBIND_NOTICE_FIELD) != agent,
            require_existing=True,
            require_write=True,
        )
    )


def rebind_notice_text(bot: str, agent: str, recorded_agent: str) -> str:
    """The notice row's English: what this chat keeps running, and how to run the new agent.

    Read by channel mirrors and the model; the dashboard draws its own copy from
    the row's token (:func:`rebind_notice_meta`).
    """
    return (
        f"ℹ {bot} is now set to {agent}, but this chat keeps running {recorded_agent}, "
        f"the agent it started with. Start a new chat with {bot} to use {agent}."
    )


def rebind_notice_meta(bot: str, agent: str, recorded_agent: str) -> dict[str, object]:
    """The notice row's ``meta``: its kind, and the token and names the dashboard words it from."""
    from kiro_crew.dashboard import gateway_notice as gn

    return {
        "kind": MEMBER_REBIND_NOTICE_KIND,
        **gn.gateway_notice_meta(
            gn.GATEWAY_NOTICE_MEMBER_REBIND_KEPT_AGENT,
            bot=bot,
            agent=agent,
            recorded=recorded_agent,
        ),
    }


async def surface_rebind_notice(
    state: Any, slot: Any, session_key: str, config: Any, bindings: Any
) -> bool:
    """Tell the user once that this chat does not run the agent its Bot is now set to.

    *bindings* are the turn's, resolved from the record it reuses. Returns
    whether a notice was given. Never raises: a notice that fails leaves the
    turn as it was.
    """
    agent, recorded_agent = rebound_agents(bindings)
    if not agent:
        return False
    try:
        bot = bot_name(config, getattr(bindings, "execution_context", None))
        if not bot or not await asyncio.to_thread(
            claim_rebind_notice, state.conversation_log, session_key, agent
        ):
            return False
        from kiro_crew.dashboard.mate_thread_wakes import is_crewmate_thread, offer_rebind_notice
        from kiro_crew.dashboard.state import append_and_surface

        append_and_surface(
            state,
            slot,
            "notice",
            rebind_notice_text(bot, agent, recorded_agent),
            "msg msg-info",
            meta=rebind_notice_meta(bot, agent, recorded_agent),
        )
        if is_crewmate_thread(slot):
            offer_rebind_notice(state, slot, agent, recorded_agent)
        return True
    except Exception:  # noqa: BLE001 - an informational notice must never fail a turn
        logger.warning(
            "rebind notice for %s could not be given", getattr(slot, "key", "?"), exc_info=True
        )
        return False
