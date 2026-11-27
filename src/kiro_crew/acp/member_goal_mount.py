"""Mount the crewmate's record tools into a Bot's own chat on a user agent.

A Bot on its own file chats on the conductor, which declares ``kirocrew-mate``. A
Bot bound to an agent of the user's own chats on THAT agent
(:func:`kiro_crew.mate_worker.member_chat_agent`), which seldom declares it, so its
chat had no ``set_goal`` though goals are set only by talking to the Bot. This adds
the server to that DM session's ``mcpServers`` array, as the crew panel is added
(``client_mcp_array._append_member_panel_server``,
``runtime_session_setup._mount_member_panel``): session-level, so the user's agent
file is untouched and every other session on it keeps its own tool set, and through
the one element writer :func:`kiro_crew.members.member_mate_session_server`, so it
carries this session's identity.

KAS is the exception: its projection declares the server in the spec it sends
(:mod:`kiro_crew.acp.kas_member_goal_tools`), and an element here as well would
register it twice. The backends are those that mount the panel
(:data:`ACP_BACKENDS_MEMBER_PANEL`): the same element writer, the same identity
carriage and the same three withholds, and every backend a Bot's DM session runs on.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Collection

from kiro_crew.acp_backends import ACP_BACKEND_KAS, ACP_BACKENDS_MEMBER_PANEL
from kiro_crew.member_mate_tools import chat_lacks_goal_tools

if TYPE_CHECKING:
    from kiro_crew.acp.client import AcpClient
    from kiro_crew.acp.runtime import AcpRuntime

logger = logging.getLogger(__name__)


def _spec_excludes_goal_server(spec: Any, session_key: str | None) -> bool:
    """Whether *spec* withholds ``kirocrew-mate`` whole (``excludedTools`` naming
    ``@kirocrew-mate``, ``managedToolPolicy.excludeServers``, a whole-server deny).

    The element appended here bypasses the spec's own exclusions, so mounting it
    then would undo what the agent's author wrote.
    """
    from kiro_crew.acp.default_crew_servers import withheld_whole
    from kiro_crew.members import MEMBER_MATE_SERVER

    if not withheld_whole(spec, [MEMBER_MATE_SERVER]):
        return False
    logger.warning(
        "member session %s: the agent spec excludes %s, so the goal tools are not mounted",
        session_key,
        MEMBER_MATE_SERVER,
    )
    return True


def append_member_goal_server(
    client: AcpClient,
    servers: list[dict[str, Any]],
    spec: Any,
    restricted: Collection[str] = (),
    disabled: Collection[str] = (),
) -> list[dict[str, Any]]:
    """*servers* with the record tools appended for a Bot's own chat, else *servers*.

    The client-composed sibling of ``_append_member_panel_server``, asking the same
    preconditions through ``_member_mount_withheld``. *spec* is the agent spec the
    array was projected from.
    """
    if client.backend not in ACP_BACKENDS_MEMBER_PANEL or client.backend == ACP_BACKEND_KAS:
        return servers
    # circular import: members' module graph is heavy; resolved at call time.
    from kiro_crew.members import (
        MEMBER_MATE_SERVER,
        is_member_session_key,
        member_mate_session_server,
    )

    if not is_member_session_key(client._session_key):
        return servers
    if not chat_lacks_goal_tools(client._agent or "", spec):
        return servers
    if _spec_excludes_goal_server(spec, client._session_key):
        return servers
    if client._member_mount_withheld(MEMBER_MATE_SERVER, "the goal tools", restricted, disabled):
        return servers
    entry = member_mate_session_server(client._session_key or "", client._stub_session_token)
    if entry is None:
        logger.warning(
            "member session %s: goal tools server unresolved -- the bot cannot set "
            "goals this session",
            client._session_key,
        )
        return servers
    return [e for e in servers if e.get("name") != entry["name"]] + [entry]


async def mount_member_goal_tools(
    runtime: AcpRuntime,
    mcp_servers: list[dict[str, Any]],
    *,
    member_session_key: str,
    agent_name: str,
    session_work_dir: Any,
    stub_token: str,
) -> list[dict[str, Any]]:
    """The runtime-composed sibling of :func:`append_member_goal_server`.

    Asked on create and resume alike, since ``session/load`` re-initializes the
    session's servers. Withheld, like the panel, when the server is switched off
    (``disabled``) or one of its tools is (``disabledTools``), and when the spec
    excludes the server whole: the element bypasses the ``tools`` allowlist and the
    spec's exclusions, so mounting it then would undo that switch.
    """
    if not member_session_key or runtime.acp_backend == ACP_BACKEND_KAS:
        return mcp_servers
    if runtime.acp_backend not in ACP_BACKENDS_MEMBER_PANEL:
        return mcp_servers
    from kiro_crew.acp.runtime_session_setup import _disable_check_scope
    from kiro_crew.acp.session_mcp import session_mcp_disabled_tools, session_mcp_server_is_disabled
    from kiro_crew.member_mate_tools import _read_template
    from kiro_crew.members import MEMBER_MATE_SERVER, member_mate_session_server

    # The spec this session consumes: its own directory first, as the projection
    # resolves it, then the user-level agents directory.
    spec = await asyncio.to_thread(_read_template, agent_name, "", session_work_dir or None)
    if not chat_lacks_goal_tools(agent_name, spec):
        return mcp_servers
    if _spec_excludes_goal_server(spec, member_session_key):
        return mcp_servers
    scope = _disable_check_scope(runtime.acp_backend, session_work_dir)
    if await asyncio.to_thread(
        session_mcp_server_is_disabled, MEMBER_MATE_SERVER, agent_name, work_dir=scope
    ):
        logger.warning(
            "member session %s: %s is switched off (disabled), so the goal tools are "
            "not mounted",
            member_session_key,
            MEMBER_MATE_SERVER,
        )
        return mcp_servers
    narrowed = await asyncio.to_thread(session_mcp_disabled_tools, agent_name, work_dir=scope)
    if any(server == MEMBER_MATE_SERVER for server, _tool in narrowed):
        logger.warning(
            "member session %s: one of %s's tools is switched off, so the goal tools "
            "are not mounted",
            member_session_key,
            MEMBER_MATE_SERVER,
        )
        return mcp_servers
    entry = await asyncio.to_thread(member_mate_session_server, member_session_key, stub_token)
    if entry is None:
        logger.warning(
            "member session %s: goal tools server unresolved -- the bot cannot set "
            "goals this session",
            member_session_key,
        )
        return mcp_servers
    return [e for e in mcp_servers if e.get("name") != entry["name"]] + [entry]
