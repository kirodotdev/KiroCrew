"""A remote MCP server's ``oauth`` block on the KAS projection.

KAS's ``mapClientMcpServers`` rebuilds each remote entry from ``url``/``headers``/
``env``/``timeout`` only, so ``oauth`` never arrives and a pre-registered client
cannot sign in. :mod:`kiro_crew.acp.kas_agents` withholds the block (it can hold a
``clientSecret``) and reports the loss through its existing session-start notice;
this leaf holds the pieces both that module and the notice read.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from typing import Any

from kiro_crew.mcp_cleanup import (
    KIROCREW_BIN_MCP_SERVERS,
    mcp_entry_is_muted,
    mcp_entry_is_registry_governed,
)

#: How a server's discarded ``oauth`` block is named in the lost-keys list, and
#: how the session notice reads the server name back out of it.
MCP_OAUTH_KEY_PREFIX = "mcpServers."
MCP_OAUTH_KEY_SUFFIX = ".oauth"

_MANAGED = frozenset(KIROCREW_BIN_MCP_SERVERS)


def registry_withheld(name: str, entry: dict[str, Any], registry_mode: bool) -> bool:
    """Whether the KAS projection withholds *entry* for the registry filter."""
    return name not in _MANAGED and (registry_mode or mcp_entry_is_registry_governed(entry))


def servers_with_discarded_oauth(
    spec: dict[str, Any], registry_governed: Callable[[], bool]
) -> list[str]:
    """Names of *spec*'s declared MCP servers whose ``oauth`` block KAS discards.

    An entry the projection never declares (muted, or withheld by the registry
    filter) is skipped: it does not run at all, so naming its sign-in would
    mislead. *registry_governed* is read only when some entry sets ``oauth``.
    """
    servers = spec.get("mcpServers")
    if not isinstance(servers, dict):
        return []
    with_oauth = [
        (name, entry)
        for name, entry in servers.items()
        if isinstance(name, str)
        and name
        and isinstance(entry, dict)
        and entry.get("oauth")
        and not mcp_entry_is_muted(entry)
    ]
    if not with_oauth:
        return []
    mode = registry_governed()
    return [name for name, entry in with_oauth if not registry_withheld(name, entry, mode)]


def oauth_lost_keys(names: Iterable[str]) -> list[str]:
    """The lost-keys entries for *names*: ``mcpServers.<name>.oauth``."""
    return [f"{MCP_OAUTH_KEY_PREFIX}{name}{MCP_OAUTH_KEY_SUFFIX}" for name in names]


def oauth_servers_in(keys: Iterable[str]) -> list[str]:
    """The server names read back out of the ``mcpServers.<name>.oauth`` *keys*."""
    pre, suf = MCP_OAUTH_KEY_PREFIX, MCP_OAUTH_KEY_SUFFIX
    return [
        k[len(pre) : -len(suf)]
        for k in keys
        if k.startswith(pre) and k.endswith(suf) and len(k) > len(pre) + len(suf)
    ]


def withhold_oauth(
    projected: dict[str, Any],
    *,
    agent_id: str,
    name: str,
    logger: logging.Logger,
) -> None:
    """Pop *projected*'s ``oauth`` block and log only how many keys it had.

    Withheld rather than sent to be discarded, because the block can hold a
    ``clientSecret``; that is also why it is not a discarded-entry key.
    """
    oauth = projected.pop("oauth", None)
    if not oauth:
        return
    # A fixed message and an integer only: nothing derived from the block's
    # keys or values reaches the log.
    count = len(oauth) if isinstance(oauth, dict) else 0
    logger.info(
        "agent %r: MCP server %r declares an oauth block (%d keys), which this "
        "backend discards, so its sign-in cannot work here; the kiro-cli "
        "backend supports it.",
        agent_id,
        name,
        count,
    )
