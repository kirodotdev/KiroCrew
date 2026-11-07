"""What the KAS engine says it accepts on the wire, and what Crew sends because of it.

An engine that announces a flag in ``initialize``'s ``agentCapabilities._meta.kiro``
takes a field older engines dropped. Each flag here turns on exactly one change
to what Crew projects; with the flag absent the projection is byte-identical to
what it was before the flag existed, so an older engine sees no difference.

- :data:`MCP_SERVER_OAUTH`: a remote MCP server's ``oauth`` block survives
  ``customAgents``. Crew then sends ``clientId``, ``redirectUri`` and
  ``clientMetadataUrl``, and ``oauthScopes`` beside them, instead of withholding
  the whole block. ``clientSecret`` is never sent over ACP: the engine does not
  accept it and keeps the raw ``customAgents`` payload in its session file.
- :data:`MCP_REGISTRY_POINTERS`: a ``{"type": "registry"}`` pointer survives both
  ``customAgents`` and the session ``mcpServers`` array (as ``_meta.kiro.type``).
  Crew then sends a user entry that carries the registry marker as a pointer
  that keeps its ``command`` and ``args`` (never its ``env``) instead of
  withholding it. Crew's own servers are never pointers: they stay
  stdio so the session-level hoist still stamps their session token.

Only a literal ``True`` counts, like ``kas_wire.CAPABILITY_CHECKPOINTS``.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from kiro_crew.mcp_cleanup import MCP_REGISTRY_TYPE

#: ``agentCapabilities._meta.kiro`` flag: ``customAgents`` keeps ``oauth``.
MCP_SERVER_OAUTH = "mcpServerOAuth"
#: ``agentCapabilities._meta.kiro`` flag: registry pointers survive the wire.
MCP_REGISTRY_POINTERS = "mcpRegistryPointers"
_KNOWN = (MCP_SERVER_OAUTH, MCP_REGISTRY_POINTERS)

#: The ``oauth`` keys the engine accepts over ACP. ``clientSecret`` is not one.
OAUTH_WIRE_KEYS = ("clientId", "redirectUri", "clientMetadataUrl")
_SCOPES_KEY = "oauthScopes"
_SECRET_KEY = "clientSecret"


def advertised(agent_capabilities: Any) -> frozenset[str]:
    """The flags of this module that *agent_capabilities* sets to literal ``True``."""
    meta = agent_capabilities.get("_meta") if isinstance(agent_capabilities, Mapping) else None
    kiro = meta.get("kiro") if isinstance(meta, Mapping) else None
    if not isinstance(kiro, Mapping):
        return frozenset()
    return frozenset(flag for flag in _KNOWN if kiro.get(flag) is True)


def advertised_by(handle: object) -> frozenset[str]:
    """:func:`advertised` for a session's ``initialize`` answer, else empty.

    *handle* is a session handle, which carries the answer itself, or a provider,
    whose ``client`` carries it.
    """
    result = getattr(handle, "initialize_result", None)
    if result is None:
        result = getattr(getattr(handle, "client", None), "initialize_result", None)
    caps = result.get("agentCapabilities") if isinstance(result, Mapping) else None
    return advertised(caps)


def _scopes(value: Any) -> list[str] | None:
    if isinstance(value, list) and all(isinstance(s, str) for s in value):
        return list(value)
    return None


def carry_oauth(
    projected: dict[str, Any], *, agent_id: str, name: str, logger: logging.Logger
) -> None:
    """Narrow *projected*'s ``oauth`` block to what the engine accepts over ACP.

    Keeps :data:`OAUTH_WIRE_KEYS` with string values, and moves a nested
    ``oauth.oauthScopes`` to the entry's own ``oauthScopes``, where it wins over a
    top-level list exactly as kiro-cli reads it. Everything else in the block is
    dropped. A ``clientSecret`` costs one log line naming the field, never its value.
    """
    oauth = projected.pop("oauth", None)
    if not isinstance(oauth, Mapping):
        return
    if oauth.get(_SECRET_KEY):
        logger.info(
            "agent %r: MCP server %r: clientSecret is not sent to this backend", agent_id, name
        )
    kept = {k: oauth[k] for k in OAUTH_WIRE_KEYS if isinstance(oauth.get(k), str) and oauth[k]}
    nested = _scopes(oauth.get(_SCOPES_KEY))
    if nested is not None:
        projected[_SCOPES_KEY] = nested
    elif _SCOPES_KEY in projected and _scopes(projected[_SCOPES_KEY]) is None:
        projected.pop(_SCOPES_KEY)
    if kept:
        projected["oauth"] = kept


def registry_pointer(entry: Mapping[str, Any]) -> dict[str, Any]:
    """The ``customAgents`` entry that defers *entry* to the account's catalog.

    It keeps the launch ``command`` and ``args``, because an engine that drops a
    marker with no command would otherwise drop the server. ``env``, ``headers``
    and every other field stay out of the pointer.
    shortcut: env is not carried; add it once the engine defines how a pointer's
    env combines with the catalog's.
    """
    pointer: dict[str, Any] = {"type": MCP_REGISTRY_TYPE}
    command = entry.get("command")
    if isinstance(command, str) and command:
        pointer["command"] = command
        args = entry.get("args")
        if isinstance(args, list) and all(isinstance(a, str) for a in args):
            pointer["args"] = list(args)
    return pointer
