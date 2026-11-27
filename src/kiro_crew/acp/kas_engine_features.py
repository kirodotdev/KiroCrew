"""What the KAS engine says it accepts on the wire, and what Crew sends because of it.

An engine that announces a flag in ``initialize``'s ``agentCapabilities._meta.kiro``
takes a field older engines dropped. Each flag here turns on exactly one change
to what Crew projects; with the flag absent the projection is byte-identical to
what it was before the flag existed, so an older engine sees no difference.

- :data:`MCP_SERVER_OAUTH`: read, but it gates nothing. KAS 0.66.104 keeps a
  remote server's ``oauth`` block and announces no flag, and older engines
  drop the block without failing the server or the
  agent (measured on 0.66.33 and 0.66.91). So Crew always sends it, narrowed by
  :func:`carry_oauth`: ``clientId``, ``redirectUri`` and ``oauthScopes``.
  ``clientMetadataUrl`` is not sent, so the engine keys every url entry's grant
  ``<url>|``, the one key :mod:`kiro_crew.mcp_grant_kas` reads.
  ``clientSecret`` is never sent in the block: KAS strips it from config on
  purpose, and older engines keep the raw ``customAgents`` payload in their
  session file. A block that carries one names a confidential client. On an
  engine without :data:`MCP_OAUTH_CLIENT_SECRET` it is withheld whole, and so is
  one that keeps nothing Crew can send (only a ``clientMetadataUrl``).
  :func:`~kiro_crew.mcp_oauth_projection.wire_oauth` is what Crew sends, read by
  this projection and the session notice alike. :func:`engine_discards_oauth`
  says when Crew knows the engine is the older kind.
- :data:`MCP_OAUTH_CLIENT_SECRET`: the engine takes a confidential client's
  secret through ``_kiro/mcp/oauth/setClientSecret`` (KAS 0.66.135, announced
  only to a client that declared ``secretStorage``). The session's own agent then
  gets its block without the secret, and the secret goes to that request after
  ``session/new`` or ``session/load`` (:mod:`kiro_crew.acp.kas_client_secret`).
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
from kiro_crew.mcp_oauth_projection import (
    CLIENT_SECRET_KEY,
    OAUTH_SCOPES_KEY,
    names_client_secret,
    sends_with_secret,
    wire_oauth,
    wire_scopes,
)

#: ``agentCapabilities._meta.kiro`` flag: ``customAgents`` keeps ``oauth``. No
#: engine announces it; parsed so one that does is still read, never a gate.
MCP_SERVER_OAUTH = "mcpServerOAuth"
#: ``agentCapabilities._meta.kiro`` flag: registry pointers survive the wire.
MCP_REGISTRY_POINTERS = "mcpRegistryPointers"
#: ``agentCapabilities._meta.kiro`` flag: the engine serves
#: ``_kiro/mcp/oauth/setClientSecret``, so a confidential client can sign in.
MCP_OAUTH_CLIENT_SECRET = "mcpOAuthClientSecret"
_KNOWN = (MCP_SERVER_OAUTH, MCP_REGISTRY_POINTERS, MCP_OAUTH_CLIENT_SECRET)

#: First engine release that keeps a ``customAgents`` server's ``oauth`` block.
OAUTH_MIN_ENGINE_VERSION: tuple[int, int, int] = (0, 66, 104)


def engine_discards_oauth(version: str | None) -> bool:
    """Whether an engine at *version* is KNOWN to drop a server's ``oauth`` block.

    Only a readable version below :data:`OAUTH_MIN_ENGINE_VERSION` counts. An
    unknown one is False: the engine may keep the block, so Crew does not claim
    it is lost.
    """
    from kiro_crew.mcp_hot_reload import parse_kiro_cli_version  # noqa: PLC0415

    parsed = parse_kiro_cli_version(version) if version else None
    return parsed is not None and parsed < OAUTH_MIN_ENGINE_VERSION


#: First engine release whose ``session/new`` answer carries no ``model`` select
#: (it sends the select in a ``config_option_update`` right after; measured on
#: 0.66.117 and 0.66.119, while 0.66.104 put it in the answer).
LATE_MODEL_SELECT_MIN_ENGINE_VERSION: tuple[int, int, int] = (0, 66, 117)


def engine_sends_model_select_late(version: str | None) -> bool:
    """Whether an engine at *version* is KNOWN to send its ``model`` select after
    ``session/new``. An unknown version (the relay route) is False.

    shortcut: the relay route's engine version is unknown, so a relay engine that
    sends the select late gets its served-default check only from a later
    ``config_option_update``; revisit when kiro-cli ships such an engine.
    """
    from kiro_crew.mcp_hot_reload import parse_kiro_cli_version  # noqa: PLC0415

    parsed = parse_kiro_cli_version(version) if version else None
    return parsed is not None and parsed >= LATE_MODEL_SELECT_MIN_ENGINE_VERSION


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


def carry_oauth(
    projected: dict[str, Any],
    *,
    agent_id: str,
    name: str,
    logger: logging.Logger,
    prereg_out: dict[str, str] | None = None,
) -> None:
    """Narrow *projected*'s ``oauth`` block to what the engine accepts over ACP.

    Sends what :func:`~kiro_crew.mcp_oauth_projection.wire_oauth` keeps, and moves a nested
    ``oauth.oauthScopes`` to the entry's own ``oauthScopes``, where it wins over a
    top-level list exactly as kiro-cli reads it. Everything else in the block is
    dropped.

    A block with a ``clientSecret`` names a confidential client. With
    *prereg_out* (the session's own agent on an engine that announced
    :data:`MCP_OAUTH_CLIENT_SECRET`) and a ``clientId`` in the block, the block is
    sent without the secret and the secret is put in *prereg_out* under
    *name*, for ``_kiro/mcp/oauth/setClientSecret``. Otherwise the engine would
    exchange the code with PKCE and no secret, so that client cannot finish a
    sign-in there, and its ``clientId`` would stop the engine's own registration
    for a server that offers it: the block is withheld whole, with one log line
    naming the field, never its value.
    """
    oauth = projected.pop("oauth", None)
    if not isinstance(oauth, Mapping):
        return
    if names_client_secret(oauth):
        if prereg_out is None or not sends_with_secret(oauth):
            logger.info(
                "agent %r: MCP server %r: its oauth block names a clientSecret, which this "
                "backend cannot use, so the block is not sent",
                agent_id,
                name,
            )
            return
        prereg_out[name] = oauth[CLIENT_SECRET_KEY]
        oauth = {k: v for k, v in oauth.items() if k != CLIENT_SECRET_KEY}
    kept, nested = wire_oauth(oauth)
    if nested is not None:
        projected[OAUTH_SCOPES_KEY] = nested
    elif OAUTH_SCOPES_KEY in projected and wire_scopes(projected[OAUTH_SCOPES_KEY]) is None:
        projected.pop(OAUTH_SCOPES_KEY)
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
