"""The stdio MCP server KAS runs for a Crew-owned Connection (``dashboard.feature_previews.crew_oauth``).

Started by :mod:`kiro_crew.kas_env_shim` from a launch snapshot that names a
``connection`` instead of a url. It is :class:`kiro_crew.kas_http_bridge.Bridge`
with one change: every message goes to the gateway's Connection proxy
(``/api/connection-proxy``), never to the vendor. This process holds no token and
no vendor url; the gateway adds the bearer
(:mod:`kiro_crew.connections.crew_oauth_proxy`).

Transport is the one ``kirocrew-secrets`` uses (:mod:`kiro_crew.mcp_secrets`), and
the answering peer must be this user's recorded gateway before any byte is sent.
On POSIX that is the owner-only dashboard Unix socket, with no TCP fallback. On
Windows, which has no Unix socket, it is loopback TCP, with the peer proven from
the kernel's table for this exact connection.

One of these runs per session per Connection, so it imports only leaf modules:
never the config loader, which composes the platform and plugin stack.
"""

from __future__ import annotations

import contextlib
import json
import sys
import urllib.error
import urllib.request
from typing import Any

from kiro_crew.kas_http_bridge import Bridge

PROXY_PATH = "/api/connection-proxy"
#: Same name as ``crew_oauth_proxy.REFUSAL_HEADER``; spelled here so the bridge
#: does not import the gateway module.
REFUSAL_HEADER = "X-Crew-Proxy-Refusal"


class _GatewayOpener:
    """``open(request, timeout)`` to this user's recorded gateway only."""

    def __init__(self, port: int) -> None:
        self._port = port

    def _verify_peer(self, sock: Any) -> None:
        from kiro_crew import cli_server_peer, platform_compat
        from kiro_crew.mcp_gateway.socketsec import PeerCredResult, check_peer_is_self

        if platform_compat.IS_WINDOWS:
            ok = (
                cli_server_peer._connected_peer_verdict(sock)
                is cli_server_peer._ConnectedPeerVerdict.MATCH
            )
        else:
            ok = check_peer_is_self(
                sock
            ) is PeerCredResult.MATCH and cli_server_peer._connected_unix_peer_is_recorded_gateway(
                sock, self._port
            )
        if not ok:
            # The gateway never sees this refusal, so it is audited here. A grant is
            # not: the proxy audits what it admits.
            with contextlib.suppress(Exception):
                cli_server_peer._audit_peer_denied(
                    "connection_bridge", self._port, "peer is not this user's recorded gateway"
                )
            # An OSError, so urllib wraps it as URLError and the bridge answers the
            # engine with an error instead of dying.
            raise PermissionError("the dashboard is not answered by this user's gateway")

    def open(self, request: urllib.request.Request, timeout: float) -> Any:
        from kiro_crew import platform_compat
        from kiro_crew.config.paths import config_dir
        from kiro_crew.dashboard.urls import dashboard_socket_name
        from kiro_crew.loopback_http import tcp_verified_urlopen, unix_socket_urlopen

        if platform_compat.IS_WINDOWS:
            return tcp_verified_urlopen(request, timeout, verify_peer=self._verify_peer)
        return unix_socket_urlopen(
            request,
            timeout,
            socket_path=config_dir() / dashboard_socket_name(self._port),
            verify_peer=self._verify_peer,
        )


class ConnectionBridge(Bridge):
    """A :class:`Bridge` whose far end is the gateway proxy."""

    def _http_error_reason(self, exc: urllib.error.HTTPError) -> str:
        # Only an answer the proxy marked as its own refusal is Kiro Crew's; any
        # other status is the provider's, relayed as-is, and is never quoted.
        if not (exc.headers and exc.headers.get(REFUSAL_HEADER)):
            return f"answered HTTP {exc.code} from the provider"
        try:
            detail = json.loads(exc.read() or b"{}").get("error")
        except (ValueError, AttributeError, OSError):
            detail = None
        if isinstance(detail, str) and detail:
            return f"was refused by Kiro Crew: {detail[:200]}"
        return f"was refused by Kiro Crew (HTTP {exc.code})"


class _AgentSocketOpener:
    """``open(request, timeout)`` over the agent socket with this process's capability.

    For a bridge under an enforced harness's credential mask, which hides the
    internal secret and the gateway record from it: it redeemed the one-time pickup
    its element carried, and the listener must be the gateway pid that element named
    (:mod:`kiro_crew.agent_ipc_client`). Never falls back to the dashboard socket.
    """

    def __init__(self, port: int) -> None:
        self._port = port

    def open(self, request: urllib.request.Request, timeout: float) -> Any:
        from kiro_crew import agent_ipc_client

        capability, sock = agent_ipc_client.current_capability(lambda: self._port)
        if not capability:
            raise urllib.error.URLError("no agent-socket capability for this Connection bridge")
        return agent_ipc_client.open_with_capability(
            request, timeout, capability=capability, socket_path=sock
        )


def build_bridge(doc: dict[str, Any], *, server: str, out: Any, err: Any) -> ConnectionBridge:
    """The bridge a verified ``connection`` snapshot describes."""
    from kiro_crew import agent_ipc_client

    port = int(doc["port"])
    headers = {
        "X-Crew-Bridge": str(doc["version"]),
        "X-Crew-Bridge-Token": str(doc["bridge_token"]),
    }
    opener: Any
    if agent_ipc_client.take_pickup():
        # Under the mask: the capability is the credential, and the gateway holds it
        # to the session it was issued for.
        agent_ipc_client.current_capability(lambda: port)
        headers["X-Session-Key"] = str(doc["session"])
        opener = _AgentSocketOpener(port)
    else:
        from kiro_crew.config.loader_locations import read_local_secret

        headers["X-Internal-Secret"] = read_local_secret(port)
        opener = _GatewayOpener(port)
    return ConnectionBridge(
        f"http://127.0.0.1:{port}{PROXY_PATH}",
        headers,
        server=server,
        out=out,
        err=err,
        opener=opener,
    )


def run_connection_bridge(doc: dict[str, Any], *, server: str) -> int:
    """Serve the engine on this process's stdio; the exit status."""
    bridge = build_bridge(doc, server=server, out=sys.stdout.buffer, err=sys.stderr)
    return bridge.serve(sys.stdin.buffer)
