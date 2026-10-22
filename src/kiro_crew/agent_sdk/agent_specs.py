"""Read a Crew agent spec from application code without importing the ACP layer.

The crewmate modules (``mate_config``, ``mate_worker``, ``mate_worker_kit``,
``mate_capability_catalog``, ``mate_config_migration``, and the heartbeat's ``goal_reminder``) read
custom-agent specs the way a session would: the hardened reader in
``acp.kas_agents`` and the per-session MCP switch-off in ``acp.session_mcp``.
Application code may not import ``kiro_crew.acp``
(``scripts/check_agent_sdk_boundary.py``), so this module is the one seam they
reach those reads through.

It translates rather than re-exports: a spec the reader refuses raises
:class:`AgentSpecUnreadable` here, not the ACP layer's
``KasAgentTranslationError``, so a consumer holds no ACP class. Every ACP import
is made at call time, both because ``kiro_crew.acp`` imports back into
``agent_sdk`` at load time and so that a test patching the ACP function on its
own module is seen here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


class AgentSpecUnreadable(ValueError):
    """An agent spec exists in a form the reader refuses, or is missing.

    The message is the reader's own, so a caller can quote it unchanged.
    """


def load_agent_spec(agents_dir: Path, agent_id: str) -> dict[str, Any]:
    """*agent_id*'s spec in *agents_dir*, as a copy the caller owns. Blocking.

    The same read a KAS session runs (``acp.kas_agents.load_agent_spec``: a spec
    declaring ``name == agent_id`` wins over the file named for it). Raises
    :class:`AgentSpecUnreadable` when there is no readable spec.
    """
    from kiro_crew.acp import kas_agents

    try:
        return kas_agents.load_agent_spec(agents_dir, agent_id)
    except kas_agents.KasAgentTranslationError as exc:
        raise AgentSpecUnreadable(str(exc)) from exc


def resolve_prompt(spec: dict[str, Any], *, agent_id: str, agents_dir: Path) -> str:
    """*spec*'s prompt as literal text, reading a ``file://`` prompt. Blocking.

    ``acp.kas_agents.resolve_prompt``: a relative path is anchored to
    *agents_dir*, and a prompt-less spec answers the KAS fallback text. Raises
    :class:`AgentSpecUnreadable` when the prompt cannot be read.
    """
    from kiro_crew.acp import kas_agents

    try:
        return kas_agents.resolve_prompt(spec, agent_id=agent_id, agents_dir=agents_dir)
    except kas_agents.KasAgentTranslationError as exc:
        raise AgentSpecUnreadable(str(exc)) from exc


def agent_spec_absent(agents_dir: Path, agent_id: str) -> bool:
    """Whether *agents_dir* holds no spec at all for *agent_id*. Blocking.

    A spec that exists but cannot be read is NOT absent. Raises ``OSError`` when
    the directory cannot be scanned (``acp.kas_agents.agent_spec_absent``).
    """
    from kiro_crew.acp import kas_agents

    return kas_agents.agent_spec_absent(agents_dir, agent_id)


def session_mcp_server_is_disabled(name: str, agent: str | None) -> bool:
    """Whether MCP server *name* is switched off whole for a session as *agent*.

    ``acp.session_mcp.session_mcp_server_is_disabled``: blocking, and never
    raises (an unreadable source switches nothing off).
    """
    from kiro_crew.acp import session_mcp

    return session_mcp.session_mcp_server_is_disabled(name, agent)
