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
import hashlib
import hmac
import http.client
import logging
import os
import re
import secrets
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
# ThreadingHTTPServer is otherwise unbounded: one incomplete
# Content-Length holds a thread until the process dies.
PROXY_MAX_INFLIGHT = 16
# Per-boot token carried only in session-inject headers. Loopback is
# same-host, not same-UID; without this the sandboxed agent can curl the
# port and receive instance-role SigV4.
PROXY_AUTH_HEADER = "X-Kirocrew-Proxy-Auth"
# Session key HMAC-bound into the auth token so a revocation of the
# originating session stops signing. Stripped hop-by-hop; never forwarded.
PROXY_SESSION_HEADER = "X-Kirocrew-Proxy-Session"
# HMAC-bound crew agent so a tighter task profile cannot be swapped
# for the surface default by rewriting this header.
PROXY_AGENT_HEADER = "X-Kirocrew-Proxy-Agent"
_GATEWAY_HOST_MARKER = ".gateway.bedrock-agentcore."
_GATEWAY_HOST_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.gateway\.bedrock-agentcore\.[a-z0-9-]+\.amazonaws\.com$"
)
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
        PROXY_AUTH_HEADER.lower(),
        PROXY_SESSION_HEADER.lower(),
        PROXY_AGENT_HEADER.lower(),
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


def is_agentcore_gateway_url(url: str) -> bool:
    """True for an https AgentCore Gateway MCP hostname.

    The proxy signs with the instance role. An arbitrary https URL would
    receive those SigV4 headers, so only
    ``*.gateway.bedrock-agentcore.<region>.amazonaws.com`` is a legal
    upstream.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.fragment:
        return False
    host = (parsed.hostname or "").lower()
    return _GATEWAY_HOST_RE.fullmatch(host) is not None


def region_from_gateway_url(url: str) -> str:
    """Return the region embedded in a Gateway MCP hostname, or env fallback."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        host = ""
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
        self.client_token = secrets.token_urlsafe(32)
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

        class _BoundedProxyServer(ThreadingHTTPServer):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                self._handler_slots = threading.BoundedSemaphore(PROXY_MAX_INFLIGHT)
                super().__init__(*args, **kwargs)

            def process_request(self, request: Any, client_address: Any) -> None:
                if not self._handler_slots.acquire(blocking=False):
                    with contextlib.suppress(OSError):
                        request.close()
                    return
                try:
                    super().process_request(request, client_address)
                except Exception:
                    self._handler_slots.release()
                    raise

            def process_request_thread(self, request: Any, client_address: Any) -> None:
                try:
                    super().process_request_thread(request, client_address)
                finally:
                    self._handler_slots.release()

        class _PreferredServer(_BoundedProxyServer):
            # Do not reuse a live listener. Windows SO_REUSEADDR would
            # otherwise succeed on an occupied preferred port and skip
            # fallback. TIME_WAIT then falls back to ephemeral, which is
            # the correct failure mode for this localhost hop.
            allow_reuse_address = False

        class _EphemeralServer(_BoundedProxyServer):
            allow_reuse_address = True

        try:
            server_cls = _EphemeralServer if preferred == 0 else _PreferredServer
            httpd = server_cls((PROXY_HOST, preferred), handler)
        except OSError:
            if preferred == 0:
                raise
            logger.info(
                "AgentCore SigV4 proxy preferred port %s in use; binding ephemeral",
                preferred,
            )
            httpd = _EphemeralServer((PROXY_HOST, 0), handler)
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
            _agentcore_headers_sent = False

            def setup(self) -> None:
                super().setup()
                self.connection.settimeout(PROXY_SOCKET_TIMEOUT_SECS)

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
                self._agentcore_headers_sent = False
                presented = self.headers.get(PROXY_AUTH_HEADER) or ""
                session_key = (self.headers.get(PROXY_SESSION_HEADER) or "").strip()
                agent = (self.headers.get(PROXY_AGENT_HEADER) or "").strip()
                expected = (
                    bound_proxy_auth_token(proxy.client_token, session_key, agent)
                    if session_key
                    else ""
                )
                if not session_key or not _auth_token_matches(presented, expected):
                    self.send_error(401, "Unauthorized")
                    return
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
                try:
                    body = self.rfile.read(length) if length else b""
                except (TimeoutError, OSError):
                    self.send_error(408, "Request Timeout")
                    return
                if length and len(body) != length:
                    self.send_error(400, "Bad Request")
                    return
                # Recheck after the body is in hand. A stalled upload would
                # otherwise keep a permit that was revoked before signing.
                if not _workload_proxy_still_permitted(
                    session_key,
                    agent=agent,
                    upstream_url=proxy.upstream_url,
                ):
                    self.send_error(403, "Forbidden")
                    return
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
                    # Headers already flushed: a second status line would
                    # append onto the MCP body the client is already reading.
                    if getattr(self, "_agentcore_headers_sent", False):
                        self.close_connection = True
                        return
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
            conn: http.client.HTTPConnection = http.client.HTTPSConnection(  # nosemgrep
                parsed.hostname or "",
                parsed.port or 443,
                timeout=PROXY_SOCKET_TIMEOUT_SECS,
                context=ssl.create_default_context(),
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
            setattr(handler, "_agentcore_headers_sent", True)
            if method != "HEAD":
                try:
                    while True:
                        chunk = resp.read1(65536)
                        if not chunk:
                            break
                        handler.wfile.write(chunk)
                        handler.wfile.flush()
                except OSError:
                    # Client or upstream dropped after headers. Do not
                    # re-raise into _handle — that path must not emit 502.
                    return
        finally:
            with contextlib.suppress(OSError):
                conn.close()


def ensure_workload_proxy(upstream_url: str) -> str | None:
    """Start (or reuse) the process-wide workload proxy. ``None`` fails closed."""
    global _PROXY
    if not is_agentcore_gateway_url(upstream_url):
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


def workload_proxy_auth_token() -> str | None:
    """Per-boot proxy token, or ``None`` when the listener is down."""
    with _LOCK:
        if _PROXY is None or not _PROXY.alive:
            return None
        return _PROXY.client_token


def bound_proxy_auth_token(client_token: str, session_key: str, agent: str = "") -> str:
    """HMAC the per-boot token with the originating session and agent."""
    material = session_key if not agent else f"{session_key}\0{agent}"
    return hmac.new(
        client_token.encode("utf-8"),
        material.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def proxy_auth_headers(session_key: str, *, agent: str = "") -> dict[str, str]:
    """Session-bound inject headers, or empty when the listener is down."""
    if not session_key:
        return {}
    token = workload_proxy_auth_token()
    if not token:
        return {}
    headers = {
        PROXY_AUTH_HEADER: bound_proxy_auth_token(token, session_key, agent),
        PROXY_SESSION_HEADER: session_key,
    }
    if agent:
        headers[PROXY_AGENT_HEADER] = agent
    return headers


def _audit_proxy_decision(session_key: str, permitted: bool, reason: str = "") -> None:
    try:
        from kiro_crew.sel import sel

        sel().log_governance_decision(
            session_key=session_key,
            tool_name="agentcore.sigv4_proxy",
            scope="capabilities.agentcore",
            outcome="allowed" if permitted else "denied",
            reason=reason,
        )
    except Exception:
        logger.debug("agentcore sigv4 proxy decision audit failed", exc_info=True)


def _workload_proxy_still_permitted(
    session_key: str, *, agent: str = "", upstream_url: str = ""
) -> bool:
    """True only when the originating session may still use this hop's proxy.

    Rechecked on every hop so a mid-process revocation of that session's
    profile stops signing even though the proxy listener is already up.
    The capability decision uses the calling session's profile (never the
    host ``_host`` surface). The policy posture must be ``workload`` —
    ``login`` uses JWT inbound, not instance IAM. *upstream_url* is the
    handling proxy's configured Gateway, not the process-wide
    ``_PROXY``: a stalled request on listener A must not authorize
    against a replacement listener B and then sign to A. Fail closed
    when *upstream_url* is empty. Both outcomes are SEL-audited.
    """
    if not session_key:
        _audit_proxy_decision("", False, reason="missing_session")
        return False
    try:
        from kiro_crew.platform.context import current_context
        from kiro_crew.platform.governance import (
            agentcore_gateway_url,
            agentcore_posture,
            resolve,
        )
        from kiro_crew.platform.governance_profiles import resolve_active_scope

        ceiling = current_context().governance
        profile = resolve_active_scope(session_key, agent=agent)
        decision = resolve(ceiling, profile, "capabilities.agentcore", "")
        permitted = bool(getattr(decision, "permitted", False))
        if not permitted:
            _audit_proxy_decision(
                session_key, False, reason=str(getattr(decision, "reason", "") or "")
            )
            return False
        if agentcore_posture(ceiling) != "workload":
            _audit_proxy_decision(session_key, False, reason="not_workload")
            return False
        current_url = (agentcore_gateway_url(ceiling) or "").rstrip()
        handling_url = (upstream_url or "").rstrip()
        if not current_url or not handling_url or handling_url != current_url:
            _audit_proxy_decision(session_key, False, reason="upstream_mismatch")
            return False
        _audit_proxy_decision(session_key, True)
        return True
    except Exception:
        _audit_proxy_decision(session_key, False, reason="recheck_failed")
        return False


def _auth_token_matches(presented: str, expected: str) -> bool:
    if not presented or not expected:
        return False
    left = presented.encode("utf-8")
    right = expected.encode("utf-8")
    if len(left) != len(right):
        return False
    return hmac.compare_digest(left, right)
