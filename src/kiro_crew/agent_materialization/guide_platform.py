"""The one-time platform grants on an existing default spec.

Two platform sets ship on every crewmate and dashboard session: the guide
(``@kirocrew-guide`` with :data:`kiro_crew.agent._GUIDE_AUTO_GRANTS`) and the
crewmate's own record (``@kirocrew-mate`` with
:data:`kiro_crew.agent._MATE_AUTO_GRANTS`). A fresh install's shipped defaults
carry both; an existing spec is given each set ONCE, the way the defaults would
have, so a user who later removes either keeps that choice. Each set has its own
marker: an install that already received the guide still receives the mate set
the first time it rebuilds on a build that ships it.
"""

from __future__ import annotations

from typing import Any

from kiro_crew import agent as agent_mod

#: One-time marker for :func:`grant_guide_platform_once`, under the data home.
GUIDE_GRANT_MARKER = "guide_platform_granted.json"
#: One-time marker for :func:`grant_mate_platform_once`, under the data home.
MATE_GRANT_MARKER = "mate_platform_granted.json"


def _marker(name: str):
    return agent_mod.config_dir() / name


def _mark(name: str) -> None:
    from kiro_crew.atomic_write import atomic_write

    atomic_write(_marker(name), '{"version": 1}\n')


def _grant_platform_set_once(
    config: dict[str, Any],
    *,
    server: str,
    grants: tuple[str, ...],
    marker: str,
    fresh_install: bool,
) -> bool:
    """Reference *server* and its *grants* on the default spec, once.

    A fresh build already carries both from the shipped defaults. An existing
    spec gains ``@<server>`` in ``tools`` and *grants* in ``allowedTools`` the
    first time, and is left as the user keeps it from then on. The caller's final
    ceiling pass still filters the grants, so a governance ceiling that denies a
    tool keeps it out.

    Returns whether the one-time grant is still to be recorded: the caller marks
    it only after the spec is written, so a write that fails leaves the grant to
    the next rebuild. A spec whose ``mcpServers`` does not carry the server yet
    gains nothing and records nothing, so the grant still lands on the rebuild
    that adds the server.
    """
    if _marker(marker).exists():
        return False
    if fresh_install:
        return True
    if server not in (config.get("mcpServers") or {}):
        return False
    ref = f"@{server}"
    tools = [t for t in config.get("tools") or [] if isinstance(t, str)]
    added = [] if ref in tools else [f"{ref} added to tools"]
    if added:
        tools.append(ref)
    config["tools"] = tools
    allowed = list(config.get("allowedTools") or [])
    granted = [g for g in grants if g not in allowed]
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


def grant_guide_platform_once(config: dict[str, Any], *, fresh_install: bool) -> bool:
    """The guide set (``@kirocrew-guide`` + :data:`agent._GUIDE_AUTO_GRANTS`), once."""
    return _grant_platform_set_once(
        config,
        server=agent_mod._GUIDE_SERVER,
        grants=agent_mod._GUIDE_AUTO_GRANTS,
        marker=GUIDE_GRANT_MARKER,
        fresh_install=fresh_install,
    )


def mark_guide_platform_granted() -> None:
    """Record that :func:`grant_guide_platform_once` has run on a written spec."""
    _mark(GUIDE_GRANT_MARKER)


def grant_mate_platform_once(config: dict[str, Any], *, fresh_install: bool) -> bool:
    """The mate set (``@kirocrew-mate`` + :data:`agent._MATE_AUTO_GRANTS`), once."""
    return _grant_platform_set_once(
        config,
        server=agent_mod._MATE_SERVER,
        grants=agent_mod._MATE_AUTO_GRANTS,
        marker=MATE_GRANT_MARKER,
        fresh_install=fresh_install,
    )


def mark_mate_platform_granted() -> None:
    """Record that :func:`grant_mate_platform_once` has run on a written spec."""
    _mark(MATE_GRANT_MARKER)
