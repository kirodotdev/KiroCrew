"""Historical ACP Agent display/prompt fallback during session restore.

A current Crew _meta.kiro envelope is authoritative, even if it declares no
Agent. This is never a permission grant: persisted tools only project display/
prompt information, not ACP authorizations.
"""
from __future__ import annotations

from typing import Any

from .agent_projection import AgentProjection


def resolve_restored_agent_projection(
    agent: AgentProjection, record: dict[str, Any], *, crew_reprojected_agents: bool,
) -> AgentProjection:
    if agent.active or crew_reprojected_agents:
        return agent
    saved_agent = record.get("agent")
    if isinstance(saved_agent, dict):
        return AgentProjection(
            agent_id=str(saved_agent.get("id") or "").strip(),
            prompt=str(saved_agent.get("prompt") or ""),
            tools=tuple(str(x) for x in (saved_agent.get("tools") or []) if isinstance(x, str)),
            tools_declared=bool(saved_agent.get("tools_declared")),
            excluded_tools=tuple(
                str(x) for x in (saved_agent.get("excludedTools") or []) if isinstance(x, str)
            ),
        )
    legacy_id = str(record.get("agent_id") or "").strip()
    if legacy_id:
        return AgentProjection(agent_id=legacy_id, prompt=str(record.get("agent_prompt") or ""))
    return agent
