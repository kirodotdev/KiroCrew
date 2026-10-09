"""The one-time platform guide grant on an existing default spec.

:func:`grant_guide_platform_once` gives an existing default spec the platform
guide set (``@kirocrew-guide`` and :data:`kiro_crew.agent._GUIDE_AUTO_GRANTS`)
once, the way a fresh install's shipped defaults already carry it, so a user who
later removes either keeps that choice.
"""

from __future__ import annotations

from typing import Any

from kiro_crew import agent as agent_mod

#: One-time marker for :func:`grant_guide_platform_once`, under the data home.
GUIDE_GRANT_MARKER = "guide_platform_granted.json"


def _marker(name: str):
    return agent_mod.config_dir() / name


def _mark(name: str) -> None:
    from kiro_crew.atomic_write import atomic_write

    atomic_write(_marker(name), '{"version": 1}\n')


def grant_guide_platform_once(config: dict[str, Any], *, fresh_install: bool) -> bool:
    """Reference the platform guide set and its grants on the default spec, once.

    A fresh build already carries both from the shipped defaults. An existing
    spec gains ``@kirocrew-guide`` in ``tools`` and
    :data:`kiro_crew.agent._GUIDE_AUTO_GRANTS` in ``allowedTools`` the first
    time, and is left as the user keeps it from then on. The caller's final
    ceiling pass still filters the grants, so a governance ceiling that denies a
    tool keeps it out.

    Returns whether the one-time grant is still to be recorded: the caller calls
    :func:`mark_guide_platform_granted` only after the spec is written, so a
    write that fails leaves the grant to the next rebuild. A spec whose
    ``mcpServers`` does not carry the guide server yet gains nothing and records
    nothing, so the grant still lands on the rebuild that adds the server.
    """
    if _marker(GUIDE_GRANT_MARKER).exists():
        return False
    if fresh_install:
        return True
    if agent_mod._GUIDE_SERVER not in (config.get("mcpServers") or {}):
        return False
    ref = f"@{agent_mod._GUIDE_SERVER}"
    tools = [t for t in config.get("tools") or [] if isinstance(t, str)]
    added = [] if ref in tools else [f"{ref} added to tools"]
    if added:
        tools.append(ref)
    config["tools"] = tools
    allowed = list(config.get("allowedTools") or [])
    granted = [g for g in agent_mod._GUIDE_AUTO_GRANTS if g not in allowed]
    allowed.extend(granted)
    config["allowedTools"] = allowed
    added += [f"{g} added to allowedTools" for g in granted]
    if added:
        # Like every other writer that adds a reference or a pre-approval to an
        # existing spec, the grant leaves an audit record.
        agent_mod.sel().log_api_access(
            caller="system",
            operation="mcp_tools_added",
            outcome="ok",
            source="install_agent",
            resources=f"{'; '.join(added)} (existing config upgrade)",
        )
    return True


def mark_guide_platform_granted() -> None:
    """Record that :func:`grant_guide_platform_once` has run on a written spec."""
    _mark(GUIDE_GRANT_MARKER)
