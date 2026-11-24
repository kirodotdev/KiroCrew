"""Shared support for the KAS agent projection tests.

Holds ``_spec``, the agent spec builder (one stock spec plus keyword overrides)
that ``test_kas_agents.py`` and ``test_kas_agents_mcp_servers.py`` both import.
"""

from __future__ import annotations


def _spec(**over):
    base = {
        "name": "kirocrew",
        "description": "the crew agent",
        "prompt": "You are Kiro.",
        "tools": ["fs_read", "fs_write", "@kirocrew-core"],
        "mcpServers": {"kirocrew-core": {"command": "x"}},
        "model": "auto",
        "includeMcpJson": False,
    }
    base.update(over)
    return base


def without_own_agents_deny(entry: dict) -> dict:
    """*entry* minus the ``subagent`` deny on Kiro Crew's own agents every batch
    entry carries (``kas_subagent_targets.deny_own_agents``)."""
    from kiro_crew.agent_files import OWNED_KIRO_AGENT_NAMES

    own = {"capability": "subagent", "match": sorted(OWNED_KIRO_AGENT_NAMES), "effect": "deny"}
    rules = [r for r in (entry.get("permissions") or {}).get("rules") or [] if r != own]
    out = {k: v for k, v in entry.items() if k != "permissions"}
    return {**out, "permissions": {**entry["permissions"], "rules": rules}} if rules else out
