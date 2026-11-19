"""The one agent Kiro Crew ships for KAS's own sub-agent tool: ``kirocrew-helper``.

A KAS sub-agent (``invoke_sub_agent``) runs inside its parent's turn under the
entry the session's ``customAgents`` batch registers for its name. This module
builds that entry for a plain helper: built-in tools only, no MCP server of its
own, no hooks, no skills. It exists only in memory, in the batch of a session
that mounts it; Kiro Crew writes no agent file for it.

Every server the session-level array carries reaches every agent of the batch,
whatever that agent's ``tools`` list names, so the helper's entry excludes each
of them whole (``@server`` in ``excludedTools``, the mechanism
``kas_subagent_targets._excluding_switched_off`` uses for a target's own
switched-off servers): Kiro Crew's own servers, the builtin app servers that run
its code, the pooled stubs, the parent's own servers and KAS's always-started
ones. The ``mcp.json`` servers are excluded at the wire envelope by the settings
stamp (``kas_settings_mcp.stamp_settings_mcp``), as for any agent that does not
opt in. So a helper reaches no Kiro Crew tool, and its calls meet the parent's
limits at Kiro Crew's gate (``session_handle_permissions``).
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

#: The helper's agent id. No agent file carries it; a user's agent file of the
#: same name never takes its place (:func:`helper_entry` builds its own).
KAS_HELPER_AGENT = "kirocrew-helper"

#: The helper's built-in tools, kiro-cli names: the default agent's built-ins
#: without the sub-agent tool, the tool loader and the session tools.
_HELPER_TOOLS = (
    "fs_read",
    "fs_write",
    "execute_bash",
    "grep",
    "glob",
    "code",
    "web_fetch",
    "web_search",
)
#: What the helper runs without asking: nothing. The shipped default agent's spec
#: lets none of the helper's built-ins run unasked either, so every helper call
#: reaches Kiro Crew's gate and is answered there as the parent's would be.
_HELPER_ALLOWED: tuple[str, ...] = ()

_HELPER_PROMPT = (
    "You are a helper that Kiro Crew's chat agent hands one short, independent task. "
    "Do that task with your built-in tools and reply with the result only. "
    "You cannot start other agents, and nothing you do outlives this reply."
)


def helper_spec() -> dict[str, Any]:
    """The helper's agent spec, in kiro-cli's shape, built fresh on every call."""
    return {
        "name": KAS_HELPER_AGENT,
        "description": "Runs one short, independent task and reports the result back.",
        "prompt": _HELPER_PROMPT,
        "tools": list(_HELPER_TOOLS),
        "allowedTools": list(_HELPER_ALLOWED),
        "mcpServers": {},
        "includeMcpJson": False,
        "resources": [],
    }


def session_level_servers(
    parent_entry: dict[str, Any], stub_server_names: Iterable[str]
) -> frozenset[str]:
    """Every server name that may reach an agent of this session's batch.

    The parent entry's own servers (hoisted to the session-level array), the
    pooled stubs, Kiro Crew's managed servers, the builtin app servers that run
    Kiro Crew's code, and KAS's always-started servers.
    """
    from kiro_crew.acp.kas_agents_mcp_servers import MANAGED_MCP_SERVER_NAMES
    from kiro_crew.acp.kas_settings_mcp import _MANDATORY_ENV
    from kiro_crew.acp.session_mcp import builtin_app_identity_launches

    declared = parent_entry.get("mcpServers")
    names = {n for n in declared if isinstance(n, str)} if isinstance(declared, dict) else set()
    names.update(n for n in stub_server_names if isinstance(n, str))
    names.update(MANAGED_MCP_SERVER_NAMES)
    names.update(builtin_app_identity_launches())
    names.update(n.strip() for n in os.environ.get(_MANDATORY_ENV, "").split(",") if n.strip())
    return frozenset(n for n in names if n)


def helper_entry(
    agents_dir: Path,
    parent_entry: dict[str, Any],
    *,
    stub_server_names: frozenset[str],
    session_key: str,
    settings_dir: str | Path | None,
) -> dict[str, Any]:
    """The helper's projected ``customAgents`` entry for this session.

    Projected through the same translation as every other agent
    (``kas_agents.build_kas_custom_agents``), then every session-level server is
    excluded whole and the ``mcp.json`` settings stamp marks it not opted in.
    """
    from kiro_crew.acp import kas_agents as kas_agents_mod
    from kiro_crew.acp.kas_settings_mcp import stamp_settings_mcp
    from kiro_crew.acp.kas_subagent_targets import _excluding_switched_off

    spec = helper_spec()
    [entry] = kas_agents_mod.build_kas_custom_agents(
        agents_dir,
        KAS_HELPER_AGENT,
        spec,
        stub_server_names=stub_server_names,
        session_key=session_key,
    )
    entry = _excluding_switched_off(entry, session_level_servers(parent_entry, stub_server_names))
    [entry] = stamp_settings_mcp([entry], KAS_HELPER_AGENT, settings_dir, opted_in=False)
    return entry


__all__ = ["KAS_HELPER_AGENT", "helper_entry", "helper_spec", "session_level_servers"]
