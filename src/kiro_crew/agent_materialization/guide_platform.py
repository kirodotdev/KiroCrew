"""The one-time platform grants on an existing default spec.

Two platform sets ship on every crewmate and dashboard session: the guide
(``@kirocrew-guide`` with :data:`kiro_crew.agent._GUIDE_AUTO_GRANTS`) and the
crewmate's own record (``@kirocrew-mate`` with
:data:`kiro_crew.agent._MATE_AUTO_GRANTS`). A fresh install's shipped defaults
carry both; an existing spec is given each set ONCE, the way the defaults would
have, so a user who later removes either keeps that choice. Each set has its own
marker: an install that already received the guide still receives the mate set
the first time it rebuilds on a build that ships it.

An edition-supplied server (``extra_mcp_servers()``) gets the same treatment per
server: one the edition starts supplying after the first install gains its
``@name`` ref in ``tools`` once, recorded by name, and never again after. A
crew's private copy of an owned template gets the refs the default spec got,
once per copy, recorded in that copy's ``agent_state`` entry.
"""

from __future__ import annotations

import json
from typing import Any

from kiro_crew import agent as agent_mod

#: One-time marker for :func:`grant_guide_platform_once`, under the data home.
GUIDE_GRANT_MARKER = "guide_platform_granted.json"
#: One-time marker for :func:`grant_mate_platform_once`, under the data home.
MATE_GRANT_MARKER = "mate_platform_granted.json"
#: The edition servers :func:`grant_edition_refs_once` has offered, under the data home.
EDITION_REFS_MARKER = "edition_mcp_refs_offered.json"


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


def _offered_edition_servers() -> set[str] | None:
    """The edition servers already offered, or ``None`` when the record is unreadable.

    An unreadable record grants nothing: re-offering every server would re-add a
    ref the user removed.
    """
    path = _marker(EDITION_REFS_MARKER)
    if not path.exists():
        return set()
    try:
        names = json.loads(path.read_text(encoding="utf-8")).get("servers")
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(names, list):
        return None
    return {n for n in names if isinstance(n, str)}


def grant_edition_refs_once(
    config: dict[str, Any],
    *,
    fresh_install: bool,
    gated_off: frozenset[str],
    mounted_aliases: dict[str, str],
) -> list[str]:
    """Reference each newly supplied edition server in ``tools``, once.

    A fresh build's ref comes from ``managed_mcp.register_managed_refs``; an
    existing spec gains ``@<name>`` (never an ``allowedTools`` grant) the first
    rebuild the server is mounted, gated off and ``opt_in`` servers excluded the way
    emission excludes them. Returns the names to record: the caller records them
    only after the spec is written, so a failed write offers them again.
    """
    from kiro_crew.agent_materialization import managed_mcp

    offered = _offered_edition_servers()
    tools = config.get("tools")
    if offered is None or not isinstance(tools, list):
        return []
    mounted = config.get("mcpServers") or {}
    pending = [
        name
        for name, spec in agent_mod._extra_mcp_servers().items()
        if name not in offered
        and name not in agent_mod._MANAGED_MCP_SERVERS
        and mounted_aliases.get(name, name) in mounted
        and managed_mcp._mcp_server_emission_eligible(name, spec, gated_off=gated_off)
    ]
    if fresh_install:
        return pending
    _append_missing_refs(tools, [mounted_aliases.get(name, name) for name in pending], "tools")
    return pending


def _append_missing_refs(tools: list, aliases: list[str], where: str) -> None:
    """Append ``@<alias>`` to *tools* for each alias it does not ref yet, with one audit record.

    A server the spec still refs, whole or as ``@name/<tool>``, keeps its refs.
    """
    added = [
        f"@{alias}"
        for alias in aliases
        if not any(
            isinstance(t, str) and (t == f"@{alias}" or t.startswith(f"@{alias}/")) for t in tools
        )
    ]
    tools.extend(added)
    if added:
        agent_mod.sel().log_api_access(
            caller="system",
            operation="mcp_tools_added",
            outcome="ok",
            source="install_agent",
            resources=f"{', '.join(added)} added to {where} (edition server newly supplied)",
        )


def mark_edition_refs_offered(names: list[str]) -> None:
    """Record that :func:`grant_edition_refs_once` offered *names* on a written spec."""
    from kiro_crew.atomic_write import atomic_write

    offered = _offered_edition_servers() or set()
    doc = {"version": 1, "servers": sorted(offered | set(names))}
    atomic_write(_marker(EDITION_REFS_MARKER), json.dumps(doc) + "\n")


def record_fork_edition_baseline(fork: str) -> None:
    """Record on a new private copy the edition servers the default spec was already offered.

    The copy carries the default spec's refs as the user kept them, so those
    servers are never offered to it again; one supplied later is offered once
    (:func:`grant_fork_edition_refs_once`). An unreadable record counts every
    server supplied now as offered, so nothing is granted on a guess.
    """
    from kiro_crew import agent_state

    offered = _offered_edition_servers()
    if offered is None:
        offered = set(agent_mod._extra_mcp_servers())
    agent_state.add_edition_refs_offered(fork, offered)


def grant_fork_edition_refs_once(
    config: dict[str, Any], fork: str, default_tools: object
) -> list[str]:
    """Give a private copy of an owned template each edition ref the default spec got, once.

    A copy made before an edition supplied a server mounts it on every fork
    refresh (``_refresh_dynamic_fields``), and its tools are callable only once
    ``tools`` names it. A server not yet offered to
    *fork*, mounted on it, and referenced whole in *default_tools* gains
    ``@<name>`` here, appended, never in ``allowedTools``. A ref the copy already
    holds, whole or as ``@name/<tool>``, is left as it is. Returns the names to
    record with ``agent_state.add_edition_refs_offered`` once the spec is written,
    so a ref the user removes afterwards stays removed.
    """
    from kiro_crew import agent_state

    tools = config.get("tools")
    if not isinstance(tools, list) or not isinstance(default_tools, list):
        return []
    try:
        offered = agent_state.get_edition_refs_offered(fork, strict=True)
    except (OSError, ValueError):
        return []
    mounted = config.get("mcpServers") or {}
    # shortcut: a name with a ``/`` is mounted under an alias that can collide
    # with a server of the user's own (the default spec then picks a suffixed
    # alias this pass cannot see), so it gains nothing here; upgrade when the
    # fork refresh carries the copy's own alias map.
    pending = [
        name
        for name in agent_mod._extra_mcp_servers()
        if "/" not in name
        and name not in offered
        and name in mounted
        and f"@{name}" in default_tools
    ]
    _append_missing_refs(tools, pending, f"{fork} tools")
    return pending
