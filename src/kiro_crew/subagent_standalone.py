"""Sub-agent readers a process may call before it has loaded ``kiro_crew.subagent``.

A stdio tool server answers ``tools/list`` before it loads the code that runs its tools,
and two of the spawn tools' descriptions carry a live value: the sub-agent ceiling and the
agents the calling parent may spawn. The two owners that compute them
(``subagent_parts.host_memory`` and ``subagent_parts.spawn_allowlists``) import for real
every name they read, so they work before the facade is loaded.

Once ``kiro_crew.subagent`` is loaded, ``_facade_compose.compose`` has replaced each owner
function with the copy that runs on the facade's globals. A module that bound an owner
function before that load would keep the replaced object, and a family that must hold one
object per name (``kiro_crew.mcp_tools.spawn``) would then hold two. So each function here
calls the owner's binding as it is at the time of the call: one object serves both sides of
the load, and a patch of ``kiro_crew.subagent.<name>`` reaches it as soon as the facade
exists. This module and the facade are the only importers of those two owners.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from kiro_crew.subagent_parts import host_memory, spawn_allowlists

if TYPE_CHECKING:
    from kiro_crew.config.loader import KiroCrewConfig


def resolve_max_subagents(cfg: KiroCrewConfig) -> int:
    """The sub-agent count ceiling for *cfg* (``subagent.resolve_max_subagents``)."""
    return host_memory.resolve_max_subagents(cfg)


def visible_agent_names(names: Iterable[str], **options: Any) -> tuple[list[str], int]:
    """A roster made safe to render and bounded (``subagent.visible_agent_names``)."""
    return spawn_allowlists.visible_agent_names(names, **options)


def parent_spawn_allowlists(
    parent_template: str, *args: Any, **options: Any
) -> tuple[tuple[str, ...], ...] | None:
    """Every ``availableAgents`` list a parent's specs declare
    (``subagent.parent_spawn_allowlists``)."""
    return spawn_allowlists.parent_spawn_allowlists(parent_template, *args, **options)


def agent_matches_allowlist(agent: str, allowlist: Iterable[str], **options: Any) -> bool:
    """Whether *agent* satisfies one allowlist (``subagent.agent_matches_allowlist``)."""
    return spawn_allowlists.agent_matches_allowlist(agent, allowlist, **options)
