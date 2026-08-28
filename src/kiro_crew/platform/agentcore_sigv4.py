"""Localhost SigV4 proxy for AgentCore Gateway IAM inbound.

kiro-cli speaks streamable HTTP MCP and does not sign ``InvokeGateway``.
Workload posture therefore points the agent at a ``127.0.0.1`` listener
in this process. Each request is re-signed with the instance credential
chain (service ``bedrock-agentcore``) and forwarded to the configured
Gateway URL only. A WAT is never a Gateway bearer.

``botocore`` is imported inside the signer so this module is safe to
import when the extra is not installed.
"""

from __future__ import annotations

import contextlib
import http.client
import logging
import os
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping
from urllib.parse import urlparse, urlunparse

logger = logging.getLogger(__name__)

# Owning-module constants (code-style).
SIGV4_SERVICE = "bedrock-agentcore"
PROXY_HOST = "127.0.0.1"
# Prefer a stable loopback port so a rebuilt kirocrew.json survives a
# gateway restart. Bind failure falls back to an ephemeral port; session
# inject then carries the live listen URL.
PROXY_PREFERRED_PORT = 18765
PROXY_PORT_ENV = "KIROCREW_AGENTCORE_PROXY_PORT"
PROXY_BODY_MAX_BYTES = 16 * 1024 * 1024
PROXY_SOCKET_TIMEOUT_SECS = 300.0
_GATEWAY_HOST_MARKER = ".gateway.bedrock-agentcore."
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
        "authorization",
        "x-amz-date",
        "x-amz-security-token",
        "x-amz-content-sha256",
        "accept-encoding",
    }
)
_ALLOWED_METHODS = frozenset({"GET", "POST", "DELETE", "HEAD"})

_LOCK = threading.Lock()
_PROXY: "GatewaySigV4Proxy | None" = None


def preferred_bind_port() -> int:
    """Port to try first: env override, else ``PROXY_PREFERRED_PORT``.

    ``0`` (env or after a refused override) means bind ephemeral. An
    out-of-range or non-integer env value is ignored.
    """
    raw = (os.environ.get(PROXY_PORT_ENV) or "").strip()
    if not raw:
        return PROXY_PREFERRED_PORT
    try:
        port = int(raw)
    except ValueError:
        return PROXY_PREFERRED_PORT
    if port == 0:
        return 0
    if 1 <= port <= 65535:
        return port
    return PROXY_PREFERRED_PORT


def region_from_gateway_url(url: str) -> str:
    """Return the region embedded in a Gateway MCP hostname, or env fallback."""
    host = (urlparse(url).hostname or "").lower()
    marker_at = host.find(_GATEWAY_HOST_MARKER)
    if marker_at >= 0:
        rest = host[marker_at + len(_GATEWAY_HOST_MARKER) :]
        region = rest.split(".", 1)[0]
        if region:
            return region
    return (os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "").strip()


def sign_aws_request(
    *,
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes,
    region: str,
    credentials: Any | None = None,
) -> dict[str, str]:
    """Return outgoing headers including a SigV4 ``Authorization``.

    *credentials* is a botocore credentials object for tests. Production
    leaves it ``None`` and uses the default chain (instance role).
    """
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.session import Session

    frozen = credentials
    if frozen is None:
        session = Session()
        raw = session.get_credentials()
        if raw is None:
            raise RuntimeError("no AWS credentials for AgentCore Gateway SigV4")
        frozen = raw.get_frozen_credentials()
    elif hasattr(frozen, "get_frozen_credentials"):
        frozen = frozen.get_frozen_credentials()
    request = AWSRequest(method=method.upper(), url=url, data=body, headers=dict(headers))
    SigV4Auth(frozen, SIGV4_SERVICE, region).add_auth(request)
    return {str(key): str(value) for key, value in request.headers.items()}


def _filter_incoming_headers(headers: Mapping[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in headers.items():
        if key.lower() in _HOP_BY_HOP:
            continue
        out[key] = value
    return out


class GatewaySigV4Proxy:
    """``127.0.0.1`` reverse proxy that SigV4-signs to one upstream Gateway URL."""

    def __init__(self, upstream_url: str, *, region: str = "", require_https: bool = True) -> None:
        parsed = urlparse(upstream_url)
        if require_https and parsed.scheme != "https":
            raise ValueError("AgentCore Gateway upstream must be https")
        if parsed.scheme not in {"https", "http"} or not parsed.netloc:
            raise ValueError("AgentCore Gateway upstream is not a usable URL")
        if parsed.username or parsed.password:
            raise ValueError("AgentCore Gateway upstream must not carry credentials")
        self.upstream_url = upstream_url.rstrip()
        self._upstream = parsed
        self._region = region or region_from_gateway_url(upstream_url)
        if not self._region:
            raise ValueError("AgentCore Gateway SigV4 needs a region")
        self._require_https = require_https
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._listen_url = ""

    @property
    def listen_url(self) -> str:
        return self._listen_url

    @property
    def alive(self) -> bool:
        return self._httpd is not None and bool(self._listen_url)

    def start(self) -> str:
        """Bind the preferred loopback port (else ephemeral). Return the listen URL."""
        if self._httpd is not None:
            return self._listen_url
        handler = self._handler_class()
        preferred = preferred_bind_port()

        class _LoopbackServer(ThreadingHTTPServer):
            # Windows defaults this False; TIME_WAIT on the preferred port
            # then fails the first bind and we fall back to ephemeral every
            # restart. POSIX already reuses. Same-uid only — the listen
            # address is 127.0.0.1.
            allow_reuse_address = True

        try:
            httpd = _LoopbackServer((PROXY_HOST, preferred), handler)
        except OSError:
            if preferred == 0:
                raise
            logger.info(
                "AgentCore SigV4 proxy preferred port %s in use; binding ephemeral",
                preferred,
            )
            httpd = _LoopbackServer((PROXY_HOST, 0), handler)
        httpd.proxy = self  # type: ignore[attr-defined]
        port = httpd.server_address[1]
        path = self._upstream.path or "/mcp"
        self._httpd = httpd
        self._listen_url = f"http://{PROXY_HOST}:{port}{path}"
        thread = threading.Thread(
            target=httpd.serve_forever,
            name="agentcore-sigv4-proxy",
            daemon=True,
        )
        self._thread = thread
        thread.start()
        return self._listen_url

    def stop(self) -> None:
        httpd = self._httpd
        self._httpd = None
        self._listen_url = ""
        if httpd is not None:
            with contextlib.suppress(OSError):
                httpd.shutdown()
            with contextlib.suppress(OSError):
                httpd.server_close()
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    def target_url(self, query: str) -> str:
        """Exact configured upstream + inbound query. Path is never client-chosen."""
        upstream = self._upstream
        return urlunparse((upstream.scheme, upstream.netloc, upstream.path, "", query, ""))

    def _handler_class(self) -> type[BaseHTTPRequestHandler]:
        proxy = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, _fmt: str, *_args: object) -> None:
                # Status only. Never headers — Authorization is SigV4 material.
                logger.debug("agentcore sigv4 proxy request")

            def do_GET(self) -> None:  # noqa: N802
                self._handle()

            def do_POST(self) -> None:  # noqa: N802
                self._handle()

            def do_DELETE(self) -> None:  # noqa: N802
                self._handle()

            def do_HEAD(self) -> None:  # noqa: N802
                self._handle()

            def _handle(self) -> None:
                method = self.command.upper()
                if method not in _ALLOWED_METHODS:
                    self.send_error(405, "Method Not Allowed")
                    return
                length_raw = self.headers.get("Content-Length") or "0"
                try:
                    length = int(length_raw)
                except ValueError:
                    self.send_error(400, "Bad Request")
                    return
                if length < 0 or length > PROXY_BODY_MAX_BYTES:
                    self.send_error(413, "Payload Too Large")
                    return
                body = self.rfile.read(length) if length else b""
                parsed = urlparse(self.path)
                target = proxy.target_url(parsed.query)
                incoming = _filter_incoming_headers(dict(self.headers))
                try:
                    signed = sign_aws_request(
                        method=method,
                        url=target,
                        headers=incoming,
                        body=body,
                        region=proxy._region,
                    )
                    proxy._forward(self, method, target, signed, body)
                except Exception:
                    logger.warning(
                        "agentcore sigv4 proxy failed to sign or forward",
                        exc_info=True,
                    )
                    self.send_error(502, "Bad Gateway")

        return _Handler

    def _forward(
        self,
        handler: BaseHTTPRequestHandler,
        method: str,
        target: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> None:
        parsed = urlparse(target)
        if self._require_https and parsed.scheme != "https":
            handler.send_error(502, "Bad Gateway")
            return
        if parsed.netloc != self._upstream.netloc or parsed.scheme != self._upstream.scheme:
            handler.send_error(502, "Bad Gateway")
            return
        if parsed.scheme == "https":
            conn: http.client.HTTPConnection = (
                http.client.HTTPSConnection(  # nosemgrep: python.lang.security.audit.httpsconnection-detected.httpsconnection-detected
                    parsed.hostname or "",
                    parsed.port or 443,
                    timeout=PROXY_SOCKET_TIMEOUT_SECS,
                    context=ssl.create_default_context(),
                )
            )
        else:
            conn = http.client.HTTPConnection(
                parsed.hostname or "",
                parsed.port or 80,
                timeout=PROXY_SOCKET_TIMEOUT_SECS,
            )
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        try:
            conn.request(method, path, body=body, headers=dict(headers))
            resp = conn.getresponse()
            handler.send_response(resp.status, resp.reason)
            for key, value in resp.getheaders():
                if key.lower() in _HOP_BY_HOP or key.lower() == "transfer-encoding":
                    continue
                handler.send_header(key, value)
            handler.send_header("Connection", "close")
            handler.end_headers()
            if method != "HEAD":
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    handler.wfile.write(chunk)
                    handler.wfile.flush()
        finally:
            with contextlib.suppress(OSError):
                conn.close()


def ensure_workload_proxy(upstream_url: str) -> str | None:
    """Start (or reuse) the process-wide workload proxy. ``None`` fails closed."""
    global _PROXY
    if not upstream_url.startswith("https://"):
        return None
    try:
        import botocore  # noqa: F401
    except ImportError:
        logger.warning("AgentCore SigV4 proxy needs botocore (kirocrew[agentcore])")
        return None
    try:
        region = region_from_gateway_url(upstream_url)
        if not region:
            raise ValueError("no region")
    except ValueError:
        logger.warning("AgentCore SigV4 proxy refused an unusable Gateway URL")
        return None
    with _LOCK:
        if _PROXY is not None and _PROXY.alive and _PROXY.upstream_url == upstream_url:
            return _PROXY.listen_url
        if _PROXY is not None:
            _PROXY.stop()
            _PROXY = None
        try:
            proxy = GatewaySigV4Proxy(upstream_url)
            listen = proxy.start()
        except Exception:
            logger.warning("AgentCore SigV4 proxy failed to start", exc_info=True)
            return None
        _PROXY = proxy
        return listen


def reset_workload_proxy() -> None:
    """Stop the process-wide proxy. Tests only."""
    global _PROXY
    with _LOCK:
        if _PROXY is not None:
            _PROXY.stop()
            _PROXY = None
