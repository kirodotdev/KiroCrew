"""A remote MCP server's ``oauth`` block on the KAS projection.

KAS engines before 0.66.104 drop the block a ``customAgents`` entry carries, so a
pre-registered client cannot sign in there; 0.66.104 keeps it. :mod:`kiro_crew.acp.kas_agents`
sends it to every engine (``kas_engine_features.carry_oauth``), narrowed by
:func:`wire_oauth`, and the session-start notice names the loss where Crew knows
the engine is older, and on any engine for a block :func:`oauth_withheld` says
sends nothing. This leaf holds the pieces both read.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
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

#: The ``oauth`` keys Crew sends over ACP. ``clientSecret`` is not one, and neither
#: is ``clientMetadataUrl``: without a ``clientId`` it changes the engine's grant key.
OAUTH_WIRE_KEYS = ("clientId", "redirectUri")
OAUTH_SCOPES_KEY = "oauthScopes"


def registry_withheld(name: str, entry: dict[str, Any], registry_mode: bool) -> bool:
    """Whether the KAS projection withholds *entry* for the registry filter."""
    return name not in _MANAGED and (registry_mode or mcp_entry_is_registry_governed(entry))


def names_client_secret(oauth: Any) -> bool:
    """Whether an ``oauth`` block names a ``clientSecret``, so KAS never receives it."""
    return isinstance(oauth, Mapping) and bool(oauth.get("clientSecret"))


def wire_scopes(value: Any) -> list[str] | None:
    """*value* as an ``oauthScopes`` list Crew sends, else None."""
    if isinstance(value, list) and all(isinstance(s, str) for s in value):
        return list(value)
    return None


def wire_oauth(oauth: Any) -> tuple[dict[str, str], list[str] | None]:
    """What Crew sends over ACP for an ``oauth`` block: the narrowed block and the
    nested ``oauthScopes`` it moves to the entry.

    The block keeps :data:`OAUTH_WIRE_KEYS` with non-empty string values. A
    non-object block, or one that names a ``clientSecret``, sends nothing:
    ``({}, None)``.
    """
    if not isinstance(oauth, Mapping) or names_client_secret(oauth):
        return {}, None
    kept = {k: oauth[k] for k in OAUTH_WIRE_KEYS if isinstance(oauth.get(k), str) and oauth[k]}
    return kept, wire_scopes(oauth.get(OAUTH_SCOPES_KEY))


def oauth_withheld(oauth: Any) -> bool:
    """Whether a set ``oauth`` block reaches no engine: :func:`wire_oauth` sends
    nothing from it (a ``clientSecret``, only a ``clientMetadataUrl``, only
    unknown keys)."""
    return bool(oauth) and wire_oauth(oauth) == ({}, None)


def servers_with_discarded_oauth(
    spec: dict[str, Any],
    registry_governed: Callable[[], bool],
    *,
    withheld_only: bool = False,
) -> list[str]:
    """Names of *spec*'s declared MCP servers whose ``oauth`` block KAS discards.

    With *withheld_only*, only the blocks :func:`oauth_withheld` names count: the
    projection sends nothing from those on any engine, while an engine that keeps
    the block receives the others.

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
        and (not withheld_only or oauth_withheld(entry["oauth"]))
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
