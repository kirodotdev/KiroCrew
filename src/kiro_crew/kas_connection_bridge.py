"""The stdio MCP server KAS runs for a Crew-owned Connection (``connections.crew_oauth``).

Started by :mod:`kiro_crew.kas_env_shim` from a launch snapshot that names a
``connection`` instead of a url. It is :class:`kiro_crew.kas_http_bridge.Bridge`
with one change: every message goes to the gateway's Connection proxy
(``/api/connection-proxy``) over the owner-only dashboard Unix socket, never to
the vendor. This process holds no token and no vendor url; the gateway adds the
bearer (:mod:`kiro_crew.connections.crew_oauth_proxy`).

Transport is the same one ``kirocrew-secrets`` uses (:mod:`kiro_crew.mcp_secrets`):
Unix socket only with no TCP fallback, and the answering peer must be this user's
recorded gateway before any byte is sent. Windows has no such channel, so the
launch refuses there.
"""

from __future__ import annotations

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
    """``open(request, timeout)`` over the attested dashboard socket only."""

    def __init__(self, port: int) -> None:
        self._port = port

    def _verify_peer(self, sock: Any) -> None:
        from kiro_crew.cli_server_peer import _connected_unix_peer_is_recorded_gateway
        from kiro_crew.mcp_gateway.socketsec import PeerCredResult, check_peer_is_self

        if check_peer_is_self(
            sock
        ) is not PeerCredResult.MATCH or not _connected_unix_peer_is_recorded_gateway(
            sock, self._port
        ):
            raise PermissionError("the dashboard socket is not answered by this user's gateway")

    def open(self, request: urllib.request.Request, timeout: float) -> Any:
        from kiro_crew.dashboard.urls import dashboard_socket_path
        from kiro_crew.loopback_http import unix_socket_urlopen

        return unix_socket_urlopen(
            request,
            timeout,
            socket_path=dashboard_socket_path(self._port),
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


def build_bridge(doc: dict[str, Any], *, server: str, out: Any, err: Any) -> ConnectionBridge:
    """The bridge a verified ``connection`` snapshot describes."""
    from kiro_crew.config.loader import read_local_secret

    port = int(doc["port"])
    headers = {
        "X-Internal-Secret": read_local_secret(port),
        "X-Crew-Bridge": str(doc["version"]),
        "X-Crew-Bridge-Token": str(doc["bridge_token"]),
    }
    return ConnectionBridge(
        f"http://127.0.0.1:{port}{PROXY_PATH}",
        headers,
        server=server,
        out=out,
        err=err,
        opener=_GatewayOpener(port),  # type: ignore[arg-type]
    )


def run_connection_bridge(doc: dict[str, Any], *, server: str) -> int:
    """Serve the engine on this process's stdio; the exit status."""
    bridge = build_bridge(doc, server=server, out=sys.stdout.buffer, err=sys.stderr)
    return bridge.serve(sys.stdin.buffer)
