"""A confidential MCP client's secret, handed to a KAS engine that takes it.

KAS 0.66.135 announces ``_meta.kiro.mcpOAuthClientSecret`` to a client that
declared ``secretStorage`` and serves ``_kiro/mcp/oauth/setClientSecret``
(kiro-agent ``mcp/oauth/client-secret-requests.ts``). It strips
``oauth.clientSecret`` from any config on purpose, so the secret has no other
way in. The projection then sends the session's own agent each confidential
block without its secret (``kas_engine_features.carry_oauth``), and this module
sends each secret after ``session/new`` or ``session/load``, naming the server
by ``sessionId`` and ``serverName``. The engine stores it in Crew's secret store
(``kiro.mcp.client-secret.<hash>``), keyed by the server's url and client id, and
reconnects the servers that use it.

The secret never reaches a log line, an error string or a notice: a failure is
logged by server name and error class or JSON-RPC code only.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from kiro_crew.acp.mcp_session_report import NAME_CAP, sanitize_sink_text

if TYPE_CHECKING:
    from kiro_crew.acp.runtime import AcpRuntime

logger = logging.getLogger(__name__)

METHOD_KAS_MCP_SET_CLIENT_SECRET = "_kiro/mcp/oauth/setClientSecret"

#: How long one ``setClientSecret`` may hold a session start. The engine answers
#: once the servers it reconnects settle or its own reset limit passes.
SET_CLIENT_SECRET_TIMEOUT_SECS = 30.0


@dataclass(frozen=True)
class PreregClient:
    """One server's secret and the client it belongs to. ``repr`` omits the secret."""

    value: str = field(repr=False)
    url: str
    client_id: str

    def digest(self) -> str:
        """Which secret this is for this client, without the secret itself."""
        material = "\0".join((self.url, self.client_id, self.value))
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


def declared_clients(
    custom_agents: Any, agent: str, collected: Mapping[str, str]
) -> dict[str, PreregClient]:
    """*collected* narrowed to the servers *agent*'s entry in *custom_agents* still
    declares with an ``oauth.clientId`` and a url: the ones the engine can name.

    A later step of the projection may drop a server the first pass kept (a
    switched-off server, a parent's limit); its secret is not sent.
    """
    if not collected or not isinstance(custom_agents, list):
        return {}
    entry = next((a for a in custom_agents if isinstance(a, dict) and a.get("id") == agent), None)
    servers = entry.get("mcpServers") if isinstance(entry, dict) else None
    if not isinstance(servers, dict):
        return {}
    out: dict[str, PreregClient] = {}
    for name, value in collected.items():
        server = servers.get(name)
        oauth = server.get("oauth") if isinstance(server, dict) else None
        client_id = oauth.get("clientId") if isinstance(oauth, dict) else None
        url = server.get("url") if isinstance(server, dict) else None
        if isinstance(client_id, str) and client_id and isinstance(url, str) and url:
            out[name] = PreregClient(value=value, url=url, client_id=client_id)
    return out


async def deliver_prereg_clients(
    runtime: AcpRuntime, session_id: str, collected: Mapping[str, PreregClient]
) -> None:
    """Send each of *collected* to the engine for *session_id*, awaited in turn.

    A client (url and client id) whose LAST secret this engine process took is the
    same one is not sent again: the engine keeps it in the store, and each send
    reconnects the servers that use it. Only the latest secret per client is
    remembered, so a secret restored after a change is sent again. A refused or
    failed send is logged and the session goes on; its server then asks for a
    sign-in that cannot finish, as it did before the engine took secrets.
    """
    sent: dict[tuple[str, str], str] = runtime._mcp_prereg_sent
    for name, target in collected.items():
        client, digest = (target.url, target.client_id), target.digest()
        if sent.get(client) == digest:
            continue
        label = sanitize_sink_text(name, NAME_CAP)
        try:
            await runtime._send_and_await(
                METHOD_KAS_MCP_SET_CLIENT_SECRET,
                {"sessionId": session_id, "serverName": name, "clientSecret": target.value},
                timeout=SET_CLIENT_SECRET_TIMEOUT_SECS,
                # No mode id to translate; the params go out exactly as built.
                translate=False,
            )
        except Exception as exc:  # noqa: BLE001 -- one server's sign-in, never the session
            # Class and code only: an engine's message is not ours to trust with a value.
            rpc_error = getattr(exc, "rpc_error", None)
            code = rpc_error.get("code") if isinstance(rpc_error, dict) else None
            logger.warning(
                "MCP server %s: the engine refused its confidential OAuth client (%s%s)",
                label,
                type(exc).__name__,
                f", code {code}" if isinstance(code, int) else "",
            )
            continue
        sent[client] = digest
        logger.info("MCP server %s: confidential OAuth client set up on the engine", label)
