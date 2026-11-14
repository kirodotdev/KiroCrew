"""The one notice a chat gives when the user's own agent lists no tools.

On the KAS path a spec with no usable ``tools`` after approved MCP additions
and default Crew servers are projected runs with no tool at all: no shell, no
file access, no MCP server, not even one its own ``mcpServers`` declares
(``acp.kas_agents._project_tools``). The notice checks those same projection
steps, because either can supply tools. :func:`surface_agent_no_tools` says it once per chat, in a
``notice`` row the dashboard words from its ``gateway_notice`` token and draws
with a link to the agent's own page under Customize, where its tools are chosen.

Only for an agent file the Custom agents tab can edit: the user's own JSON file
in the agents directory. A folder agent, a package's or Kiro Crew's own agent, a
markdown agent and a Bot's file get no row, because the link would open a page
that cannot change them. A Bot's chat gets none either, whatever file it runs:
the Bot's projection adds its own dashboard tools (:func:`_is_bot_slot`). "Once" is a compare-and-set of
:data:`AGENT_NO_TOOLS_NOTICE_FIELD` on the session's metadata line, so a Reload,
a resume or a restart does not repeat it, and a chat switched to another such
agent is told about that one too.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from kiro_crew.members import DM_SLOT_MODE

logger = logging.getLogger(__name__)

#: ``meta.kind`` of the notice row.
AGENT_NO_TOOLS_NOTICE_KIND = "agent_no_tools"
#: The metadata-line field naming the agent this chat was last told about.
AGENT_NO_TOOLS_NOTICE_FIELD = "agent_no_tools_notice"
#: ``slot.agent_kind`` of a chat that runs a Bot.
_MEMBER_KIND = "member"


def _is_bot_slot(slot: Any) -> bool:
    """Whether *slot* runs a Bot, whose projection adds its own dashboard tools.

    A Bot's DM slot carries ``mode == DM_SLOT_MODE`` and no ``agent_kind``
    (``handlers.members_thread``), read the way ``turn_admission`` reads it; a
    chat that picked a Bot carries ``agent_kind == "member"``.
    """
    return getattr(slot, "mode", "") == DM_SLOT_MODE or (
        getattr(slot, "agent_kind", "") == _MEMBER_KIND
    )


def spec_lists_tools(spec: dict[str, Any]) -> bool:
    """Whether *spec*'s ``tools`` names at least one tool, as the KAS projection reads it.

    ``"*"`` and a list holding a non-empty string do. An absent key, ``[]`` and
    any other value do not: the projection sends an empty list for each
    (``acp.kas_agents._project_tools``; ``test_agent_no_tools_notice`` pins the two
    readings together). An explicit ``[]`` is treated like an absent key, because
    the session it gives is the same.
    """
    raw = spec.get("tools")
    if raw == "*":
        return True
    return isinstance(raw, list) and any(isinstance(t, str) and t for t in raw)


def session_spec_lists_tools(agent: str, spec: dict[str, Any]) -> bool:
    """Whether the KAS session's copy lists tools after additions and defaults.

    Shared by the chat notice and the editor's detail read. Blocking file I/O;
    the projection never writes the added servers or grants into the agent file.
    """
    from kiro_crew.agent_sdk import spec_with_default_crew_servers
    from kiro_crew.template_mcp_additions import merge_at_spawn

    # The tools projection order used by acp.harness.kas.resolve_projected_spec.
    projected = spec_with_default_crew_servers(agent, merge_at_spawn(agent, spec))
    return spec_lists_tools(projected)


def editable_agent_without_tools(agent: str) -> bool:
    """Whether *agent* is one file the Custom agents tab can edit, and it lists no tools.

    Resolved the way that tab's save resolves a name (``_agent_detail_candidates``,
    ``read_only_reason_for_path``), so a row is only ever written for an agent the
    linked page can change. Blocking file I/O.
    """
    # circular import: the handlers facade imports the chat runner at load time.
    from kiro_crew.dashboard.handlers import agents as agent_handlers

    candidates = agent_handlers._agent_detail_candidates(agent)
    if len(candidates) != 1:
        return False
    path, spec = candidates[0]
    # A Bot's own file lives in the crewmates directory and is edited on its page.
    if path.parent != agent_handlers.kiro_agents_dir_path():
        return False
    if agent_handlers.read_only_reason_for_path(path) is not None:
        return False
    return not session_spec_lists_tools(agent, spec)


def claim_agent_no_tools_notice(conversation_log: Any, session_key: str, agent: str) -> bool:
    """Record that *session_key* is told about *agent*. True only for the first claim.

    A session with no transcript, or a line that cannot be written, claims
    nothing, so it is told nothing. Blocking file I/O.
    """
    return bool(
        conversation_log.update_metadata_if(
            session_key,
            {AGENT_NO_TOOLS_NOTICE_FIELD: agent},
            lambda metadata: metadata.get(AGENT_NO_TOOLS_NOTICE_FIELD) != agent,
            require_existing=True,
            require_write=True,
        )
    )


def agent_no_tools_text(agent: str) -> str:
    """The notice row's English, for channel mirrors and the model.

    The dashboard draws its own copy from the row's token (:func:`agent_no_tools_meta`).
    """
    return (
        f"ℹ Agent {agent} lists no tools, so it cannot run commands, work with files "
        "or use MCP servers in this chat. Choose its tools under Customize, Custom agents."
    )


def agent_no_tools_meta(agent: str) -> dict[str, object]:
    """The notice row's ``meta``: its kind, and the token and name the dashboard words it from."""
    from kiro_crew.dashboard import gateway_notice as gn

    return {
        "kind": AGENT_NO_TOOLS_NOTICE_KIND,
        **gn.gateway_notice_meta(gn.GATEWAY_NOTICE_AGENT_NO_TOOLS, agent=agent),
    }


async def surface_agent_no_tools(state: Any, slot: Any, session_key: str, agent: str) -> bool:
    """Tell the user once that *agent*, their own agent file, lists no tools.

    Called for a new KAS session. Returns whether a notice was given. Never
    raises: a notice that fails leaves the turn as it was.
    """
    if not agent or not session_key or _is_bot_slot(slot):
        return False
    try:
        if not await asyncio.to_thread(editable_agent_without_tools, agent):
            return False
        if not await asyncio.to_thread(
            claim_agent_no_tools_notice, state.conversation_log, session_key, agent
        ):
            return False
        from kiro_crew.dashboard.state import append_and_surface

        append_and_surface(
            state,
            slot,
            "notice",
            agent_no_tools_text(agent),
            "msg msg-info",
            meta=agent_no_tools_meta(agent),
        )
        return True
    except Exception:  # noqa: BLE001 - an informational notice must never fail a turn
        logger.warning(
            "no-tools notice for %s could not be given", getattr(slot, "key", "?"), exc_info=True
        )
        return False
