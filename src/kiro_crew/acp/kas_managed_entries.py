"""Resolve the managed invocation of each Crew server the active KAS agent declares.

Split out of :mod:`kiro_crew.acp.kas_agents` for its line cap; that module keeps
the import path (``kas_agents.resolve_managed_entries``), and its hoist imports the
per-server relaunch helpers below.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from kiro_crew.acp.kas_app_identity import TOKENED_ENV_KEYS_KEPT
from kiro_crew.mcp_cleanup import KIROCREW_BIN_MCP_SERVERS

_MANAGED_MCP_SERVER_NAMES = frozenset(KIROCREW_BIN_MCP_SERVERS)

logger = logging.getLogger(__name__)


def resolve_managed_entries(
    custom_agents: list[dict[str, Any]] | None,
    agent_id: str,
) -> dict[str, Any]:
    """The managed invocation for each managed server the ACTIVE agent declares.

    Blocking: :func:`~kiro_crew.agent.managed_mcp_spec_entry` resolves the
    ``kirocrew`` launcher with filesystem and ``PATH`` probes. Call it off the
    event loop and hand the result to
    :func:`~kiro_crew.acp.kas_agents.hoist_servers_for_session`, which then stays
    free of I/O. A name whose invocation does not resolve maps to ``None``, the
    same answer the in-place lookup gives.
    """
    from kiro_crew.agent import managed_mcp_spec_entry

    names: set[str] = set()
    for descriptor in custom_agents or ():
        if not isinstance(descriptor, dict) or descriptor.get("id") != agent_id:
            continue
        declared = descriptor.get("mcpServers")
        if isinstance(declared, dict):
            names.update(name for name in declared if name in _MANAGED_MCP_SERVER_NAMES)
    return {name: managed_mcp_spec_entry(name, include_opt_in=True) for name in sorted(names)}


def managed_entry(name: str, managed_entries: Mapping[str, Any] | None) -> Any:
    """*name*'s managed invocation: the one resolved off the loop, else resolved here."""
    if managed_entries is not None and name in managed_entries:
        return managed_entries[name]
    from kiro_crew.agent import managed_mcp_spec_entry

    return managed_mcp_spec_entry(name, include_opt_in=True)


def tokened_managed_element(
    element: dict[str, Any],
    session_token: str,
    attach: Any,
    managed_entries: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """*element* relaunched as the managed invocation and stamped with the token.

    ``None`` from the managed source (name not managed, closed ``spec_gate``,
    unresolvable invocation) returns *element* untouched and untokened: that is
    the verdict ``gatewayd._spawns_own_control_plane`` reaches for the same name,
    so nothing here hands out a token the daemon would refuse.
    """
    from kiro_crew.agent import _managed_mcp_env

    name = str(element.get("name", ""))
    managed = managed_entry(name, managed_entries)
    if not isinstance(managed, dict) or not managed.get("command"):
        logger.warning(
            "hoisted MCP server %r gets no session token: its managed invocation does"
            " not resolve, so the launch the spec declares cannot be attested",
            name,
        )
        return element
    managed_cmd = str(managed["command"])
    managed_args = [str(a) for a in managed.get("args", [])]
    declared_args = element.get("args")
    if element.get("command") != managed_cmd or declared_args != managed_args:
        # Neither value is logged: the declared args may carry a secret, and the
        # managed invocation is enough to find the entry.
        logger.warning(
            "hoisted MCP server %r declares a launch that is not the managed"
            " invocation; launching the managed one so the session token only"
            " reaches Kiro Crew's own binary",
            name,
        )
    raw_env = element.get("env")
    pairs: list[Any] = raw_env if isinstance(raw_env, list) else []
    env = {
        str(pair["name"]): str(pair.get("value", ""))
        for pair in pairs
        if isinstance(pair, dict) and pair.get("name") in TOKENED_ENV_KEYS_KEPT
    }
    env.update(_managed_mcp_env())
    relaunched = dict(element)
    relaunched["command"] = managed_cmd
    relaunched["args"] = managed_args
    relaunched["env"] = [{"name": k, "value": v} for k, v in env.items()]
    (stamped,) = attach([relaunched], session_token)
    return stamped
