"""Localhost SigV4 proxy for AgentCore Gateway IAM inbound."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest


def test_region_from_gateway_url(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.platform.agentcore_sigv4 import region_from_gateway_url

    assert (
        region_from_gateway_url("https://abc.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp")
        == "us-west-2"
    )
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-1")
    assert region_from_gateway_url("https://example.test/mcp") == "eu-west-1"
    assert region_from_gateway_url("https://[") == "eu-west-1"


def test_malformed_persisted_url_is_not_a_gateway() -> None:
    """A reserved MCP entry with a broken IPv6 literal must not abort rebuild."""
    from kiro_crew.platform.agentcore_sigv4 import is_agentcore_gateway_url

    assert is_agentcore_gateway_url("https://[") is False
    assert is_agentcore_gateway_url("https://") is False
    assert (
        is_agentcore_gateway_url(
            "https://abc.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp"
        )
        is True
    )


def test_sigv4_service_and_listen_host() -> None:
    from kiro_crew.platform.agentcore_sigv4 import PROXY_HOST, SIGV4_SERVICE

    assert SIGV4_SERVICE == "bedrock-agentcore"
    assert PROXY_HOST == "127.0.0.1"


def test_preferred_bind_port(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.platform.agentcore_sigv4 import (
        PROXY_PORT_ENV,
        PROXY_PREFERRED_PORT,
        preferred_bind_port,
    )

    monkeypatch.delenv(PROXY_PORT_ENV, raising=False)
    assert preferred_bind_port() == PROXY_PREFERRED_PORT
    monkeypatch.setenv(PROXY_PORT_ENV, "19001")
    assert preferred_bind_port() == 19001
    monkeypatch.setenv(PROXY_PORT_ENV, "0")
    assert preferred_bind_port() == 0
    monkeypatch.setenv(PROXY_PORT_ENV, "nope")
    assert preferred_bind_port() == PROXY_PREFERRED_PORT
    monkeypatch.setenv(PROXY_PORT_ENV, "99999")
    assert preferred_bind_port() == PROXY_PREFERRED_PORT


def test_preferred_port_server_does_not_reuse_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live occupier must force ephemeral fallback; Windows SO_REUSEADDR would steal."""
    from kiro_crew.platform.agentcore_sigv4 import PROXY_PORT_ENV, GatewaySigV4Proxy

    tmp = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    port = tmp.server_address[1]
    tmp.server_close()
    monkeypatch.setenv(PROXY_PORT_ENV, str(port))
    proxy = GatewaySigV4Proxy(
        "http://127.0.0.1:9/mcp",
        region="us-east-1",
        require_https=False,
    )
    try:
        listen = proxy.start()
        httpd = proxy._httpd
        assert httpd is not None
        assert listen == f"http://127.0.0.1:{port}/mcp"
        assert httpd.allow_reuse_address is False
    finally:
        proxy.stop()


def test_proxy_uses_preferred_port_when_free(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.platform.agentcore_sigv4 import PROXY_PORT_ENV, GatewaySigV4Proxy

    tmp = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    port = tmp.server_address[1]
    tmp.server_close()
    monkeypatch.setenv(PROXY_PORT_ENV, str(port))
    proxy = GatewaySigV4Proxy(
        "http://127.0.0.1:9/mcp",
        region="us-east-1",
        require_https=False,
    )
    try:
        listen = proxy.start()
        assert listen == f"http://127.0.0.1:{port}/mcp"
    finally:
        proxy.stop()


def test_proxy_falls_back_when_preferred_port_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.platform.agentcore_sigv4 import PROXY_PORT_ENV, GatewaySigV4Proxy

    blocker = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    occupied = blocker.server_address[1]
    monkeypatch.setenv(PROXY_PORT_ENV, str(occupied))
    proxy = GatewaySigV4Proxy(
        "http://127.0.0.1:9/mcp",
        region="us-east-1",
        require_https=False,
    )
    try:
        listen = proxy.start()
        assert listen.startswith("http://127.0.0.1:")
        bound = int(listen.rsplit(":", 1)[1].split("/", 1)[0])
        assert bound != occupied
    finally:
        proxy.stop()
        blocker.server_close()


def test_sign_aws_request_adds_sigv4_headers() -> None:
    pytest.importorskip("botocore")
    from botocore.credentials import Credentials

    from kiro_crew.platform.agentcore_sigv4 import SIGV4_SERVICE, sign_aws_request

    headers = sign_aws_request(
        method="POST",
        url="https://abc.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp",
        headers={"Content-Type": "application/json"},
        body=b"{}",
        region="us-east-1",
        credentials=Credentials(
            "AKIAIOSFODNN7EXAMPLE",
            "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        ),
    )
    auth = headers.get("Authorization") or headers.get("authorization")
    assert auth is not None
    assert auth.startswith("AWS4-HMAC-SHA256")
    assert SIGV4_SERVICE in auth
    assert "us-east-1" in auth


def test_target_url_never_takes_client_path() -> None:
    from kiro_crew.platform.agentcore_sigv4 import GatewaySigV4Proxy

    proxy = GatewaySigV4Proxy(
        "https://abc.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp",
        region="us-east-1",
    )
    assert (
        proxy.target_url("session=1")
        == "https://abc.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp?session=1"
    )
    assert (
        proxy.target_url("") == "https://abc.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
    )


def test_proxy_bounds_inflight_and_inbound_reads() -> None:
    """An authenticated stall must not hold an unbounded handler thread."""
    import inspect

    from kiro_crew.platform import agentcore_sigv4 as sigv4

    start_src = inspect.getsource(sigv4.GatewaySigV4Proxy.start)
    handle_src = inspect.getsource(sigv4.GatewaySigV4Proxy._handler_class)
    assert "BoundedSemaphore(PROXY_MAX_INFLIGHT)" in start_src
    assert "self.connection.settimeout(PROXY_SOCKET_TIMEOUT_SECS)" in handle_src
    assert 'send_error(408, "Request Timeout")' in handle_src
    read_at = handle_src.index("self.rfile.read")
    recheck_at = handle_src.index("_workload_proxy_still_permitted")
    assert read_at < recheck_at


def test_proxy_streams_upstream_with_read1() -> None:
    """``read(n)`` waits for n bytes or EOF; a small SSE frame would stall MCP."""
    import inspect

    from kiro_crew.platform import agentcore_sigv4 as sigv4

    src = inspect.getsource(sigv4)
    assert "resp.read1(65536)" in src
    assert "resp.read(65536)" not in src


def test_proxy_signs_and_forwards_to_local_upstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.platform import agentcore_sigv4 as sigv4

    seen: dict[str, Any] = {}

    class _Upstream(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:
            return

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or "0")
            seen["path"] = self.path
            seen["body"] = self.rfile.read(length)
            seen["authorization"] = self.headers.get("Authorization")
            seen["x-test-signed"] = self.headers.get("X-Test-Signed")
            seen["x-kirocrew-proxy-auth"] = self.headers.get("X-Kirocrew-Proxy-Auth")
            seen["x-kirocrew-proxy-session"] = self.headers.get("X-Kirocrew-Proxy-Session")
            payload = json.dumps({"ok": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    thread = Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    host, port = upstream.server_address[:2]
    upstream_url = f"http://{host}:{port}/mcp"

    def _fake_sign(**kwargs: Any) -> dict[str, str]:
        headers = dict(kwargs["headers"])
        headers["X-Test-Signed"] = "1"
        headers["Authorization"] = "AWS4-HMAC-SHA256 Credential=test"
        return headers

    monkeypatch.setattr(sigv4, "sign_aws_request", _fake_sign)
    monkeypatch.setattr(sigv4, "_workload_proxy_still_permitted", lambda session_key="", **_k: True)
    proxy = sigv4.GatewaySigV4Proxy(upstream_url, region="us-east-1", require_https=False)
    try:
        listen = proxy.start()
        assert listen.startswith("http://127.0.0.1:")
        session_key = "agent:main:main"
        bare = Request(
            listen,
            data=b'{"jsonrpc":"2.0"}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(HTTPError) as denied:
            urlopen(bare, timeout=5)  # noqa: S310  # nosemgrep
        assert denied.value.code == 401
        raw_token = Request(
            listen,
            data=b'{"jsonrpc":"2.0"}',
            headers={
                "Content-Type": "application/json",
                sigv4.PROXY_AUTH_HEADER: proxy.client_token,
            },
            method="POST",
        )
        with pytest.raises(HTTPError) as raw_denied:
            urlopen(raw_token, timeout=5)  # noqa: S310  # nosemgrep
        assert raw_denied.value.code == 401
        req = Request(
            listen,
            data=b'{"jsonrpc":"2.0"}',
            headers={
                "Content-Type": "application/json",
                sigv4.PROXY_AUTH_HEADER: sigv4.bound_proxy_auth_token(
                    proxy.client_token, session_key
                ),
                sigv4.PROXY_SESSION_HEADER: session_key,
            },
            method="POST",
        )
        with urlopen(req, timeout=5) as resp:  # noqa: S310  # nosemgrep
            body = json.loads(resp.read().decode())
        assert body == {"ok": True}
        assert seen["path"] == "/mcp"
        assert seen["body"] == b'{"jsonrpc":"2.0"}'
        assert seen["x-test-signed"] == "1"
        assert seen["authorization"] == "AWS4-HMAC-SHA256 Credential=test"
        assert seen.get("x-kirocrew-proxy-auth") is None
        assert seen.get("x-kirocrew-proxy-session") is None
    finally:
        proxy.stop()
        upstream.shutdown()
        upstream.server_close()


def test_proxy_does_not_send_error_after_headers_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mid-stream abort must close the hop, not append a second 502."""
    import inspect

    from kiro_crew.platform import agentcore_sigv4 as sigv4

    handle_src = inspect.getsource(sigv4.GatewaySigV4Proxy._handler_class)
    forward_src = inspect.getsource(sigv4.GatewaySigV4Proxy._forward)
    except_at = handle_src.index("except Exception:")
    assert handle_src.index("_agentcore_headers_sent", except_at) < handle_src.index(
        "send_error(502", except_at
    )
    assert "self.close_connection = True" in handle_src[except_at:]
    assert forward_src.index("end_headers") < forward_src.index("_agentcore_headers_sent")

    class _Upstream(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:
            return

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or "0")
            self.rfile.read(length)
            payload = json.dumps({"ok": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    thread = Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    host, port = upstream.server_address[:2]
    upstream_url = f"http://{host}:{port}/mcp"

    def _fake_sign(**kwargs: Any) -> dict[str, str]:
        headers = dict(kwargs["headers"])
        headers["Authorization"] = "AWS4-HMAC-SHA256 Credential=test"
        return headers

    monkeypatch.setattr(sigv4, "sign_aws_request", _fake_sign)
    monkeypatch.setattr(sigv4, "_workload_proxy_still_permitted", lambda session_key="", **_k: True)
    proxy = sigv4.GatewaySigV4Proxy(upstream_url, region="us-east-1", require_https=False)
    errors: list[int] = []
    orig_factory = proxy._handler_class

    def tracking_factory() -> type[BaseHTTPRequestHandler]:
        cls = orig_factory()
        orig_send_error = cls.send_error

        def tracked(
            self: BaseHTTPRequestHandler,
            code: int,
            message: str | None = None,
            explain: str | None = None,
        ) -> None:
            errors.append(code)
            return orig_send_error(self, code, message, explain)

        cls.send_error = tracked  # type: ignore[method-assign]
        return cls

    monkeypatch.setattr(proxy, "_handler_class", tracking_factory)
    orig_forward = proxy._forward

    def explode_after_stream(
        handler: BaseHTTPRequestHandler,
        method: str,
        target: str,
        headers: Any,
        body: bytes,
    ) -> None:
        orig_forward(handler, method, target, headers, body)
        raise RuntimeError("late failure after headers")

    monkeypatch.setattr(proxy, "_forward", explode_after_stream)
    try:
        listen = proxy.start()
        session_key = "agent:main:main"
        req = Request(
            listen,
            data=b'{"jsonrpc":"2.0"}',
            headers={
                "Content-Type": "application/json",
                sigv4.PROXY_AUTH_HEADER: sigv4.bound_proxy_auth_token(
                    proxy.client_token, session_key
                ),
                sigv4.PROXY_SESSION_HEADER: session_key,
            },
            method="POST",
        )
        try:
            with urlopen(req, timeout=5) as resp:  # noqa: S310  # nosemgrep
                resp.read()
        except (OSError, HTTPError):
            pass
        assert 502 not in errors
    finally:
        proxy.stop()
        upstream.shutdown()
        upstream.server_close()


def test_proxy_sends_502_when_sign_fails_before_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-header failure still answers 502; the close-only path is after flush."""
    from kiro_crew.platform import agentcore_sigv4 as sigv4

    def _boom(**_kwargs: Any) -> dict[str, str]:
        raise RuntimeError("no credentials")

    monkeypatch.setattr(sigv4, "sign_aws_request", _boom)
    monkeypatch.setattr(sigv4, "_workload_proxy_still_permitted", lambda session_key="", **_k: True)
    proxy = sigv4.GatewaySigV4Proxy(
        "http://127.0.0.1:9/mcp",
        region="us-east-1",
        require_https=False,
    )
    try:
        listen = proxy.start()
        session_key = "agent:main:main"
        req = Request(
            listen,
            data=b"{}",
            headers={
                "Content-Type": "application/json",
                sigv4.PROXY_AUTH_HEADER: sigv4.bound_proxy_auth_token(
                    proxy.client_token, session_key
                ),
                sigv4.PROXY_SESSION_HEADER: session_key,
            },
            method="POST",
        )
        with pytest.raises(HTTPError) as failed:
            urlopen(req, timeout=5)  # noqa: S310  # nosemgrep
        assert failed.value.code == 502
    finally:
        proxy.stop()


def test_proxy_refuses_after_capability_revoked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.platform import agentcore_sigv4 as sigv4

    class _Upstream(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:
            return

        def do_POST(self) -> None:  # noqa: N802
            self.send_error(500, "should not be reached")

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    thread = Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    host, port = upstream.server_address[:2]
    monkeypatch.setattr(sigv4, "sign_aws_request", lambda **_k: {})
    monkeypatch.setattr(
        sigv4, "_workload_proxy_still_permitted", lambda session_key="", **_k: False
    )
    proxy = sigv4.GatewaySigV4Proxy(
        f"http://{host}:{port}/mcp", region="us-east-1", require_https=False
    )
    try:
        listen = proxy.start()
        session_key = "agent:main:main"
        req = Request(
            listen,
            data=b"{}",
            headers={
                "Content-Type": "application/json",
                sigv4.PROXY_AUTH_HEADER: sigv4.bound_proxy_auth_token(
                    proxy.client_token, session_key
                ),
                sigv4.PROXY_SESSION_HEADER: session_key,
            },
            method="POST",
        )
        with pytest.raises(HTTPError) as denied:
            urlopen(req, timeout=5)  # noqa: S310  # nosemgrep
        assert denied.value.code == 403
    finally:
        proxy.stop()
        upstream.shutdown()
        upstream.server_close()


def test_ensure_workload_proxy_refuses_non_https() -> None:
    from kiro_crew.platform.agentcore_sigv4 import ensure_workload_proxy, reset_workload_proxy

    reset_workload_proxy()
    assert ensure_workload_proxy("http://127.0.0.1/mcp") is None
    reset_workload_proxy()


def test_ensure_workload_proxy_refuses_non_gateway_https() -> None:
    from kiro_crew.platform.agentcore_sigv4 import ensure_workload_proxy, reset_workload_proxy

    reset_workload_proxy()
    assert ensure_workload_proxy("https://evil.example.test/mcp") is None
    reset_workload_proxy()


_LIVE_GATEWAY = "https://gw.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"


def _permit_workload(monkeypatch: pytest.MonkeyPatch, *, permitted: bool = True) -> list[str]:
    """Stub session-profile resolve + workload posture. Returns scoped keys."""
    seen: list[str] = []

    class _Decision:
        reason = ""

        def __init__(self) -> None:
            self.permitted = permitted

    def _scope(session_key: str, **kwargs: object) -> str:
        seen.append((session_key, str(kwargs.get("agent") or "")))
        return session_key

    monkeypatch.setattr(
        "kiro_crew.platform.governance_profiles.resolve_active_scope",
        _scope,
    )
    monkeypatch.setattr(
        "kiro_crew.platform.governance.resolve",
        lambda _ceiling, _profile, *_a, **_k: _Decision(),
    )
    monkeypatch.setattr(
        "kiro_crew.platform.governance.agentcore_posture",
        lambda _gov: "workload",
    )

    class _Gov:
        agentcore_gateway_url = _LIVE_GATEWAY

    class _Ctx:
        governance = _Gov()

    class _Live:
        upstream_url = _LIVE_GATEWAY

    monkeypatch.setattr("kiro_crew.platform.context.current_context", lambda: _Ctx())
    monkeypatch.setattr("kiro_crew.platform.agentcore_sigv4._PROXY", _Live())
    return seen


def test_proxy_recheck_audits_originating_session(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.platform import agentcore_sigv4 as sigv4
    from kiro_crew.sel import sel

    seen = _permit_workload(monkeypatch)
    assert sigv4._workload_proxy_still_permitted("dashboard:1", upstream_url=_LIVE_GATEWAY) is True
    assert seen == [("dashboard:1", "")]
    events = [
        e
        for e in sel().recent(limit=50)
        if e.get("operation") == "agentcore.sigv4_proxy"
        and e.get("caller_identity") == "dashboard:1"
    ]
    assert events
    assert events[0].get("outcome") == "allowed"


def test_proxy_recheck_refuses_login_posture(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.platform import agentcore_sigv4 as sigv4

    _permit_workload(monkeypatch)
    monkeypatch.setattr(
        "kiro_crew.platform.governance.agentcore_posture",
        lambda _gov: "login",
    )
    assert sigv4._workload_proxy_still_permitted("dashboard:1", upstream_url=_LIVE_GATEWAY) is False


def test_proxy_recheck_uses_calling_session_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.platform import agentcore_sigv4 as sigv4

    seen = _permit_workload(monkeypatch, permitted=False)
    assert sigv4._workload_proxy_still_permitted("slack:U0123", agent="researcher") is False
    assert seen == [("slack:U0123", "researcher")]


def test_proxy_recheck_refuses_replaced_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.platform import agentcore_sigv4 as sigv4

    _permit_workload(monkeypatch)
    monkeypatch.setattr(
        "kiro_crew.platform.governance.agentcore_gateway_url",
        lambda _gov: "https://other.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp",
    )
    assert sigv4._workload_proxy_still_permitted("dashboard:1", upstream_url=_LIVE_GATEWAY) is False


def test_proxy_recheck_validates_handling_upstream_not_global(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stalled hop on A must not authorize because the live listener is B."""
    from kiro_crew.platform import agentcore_sigv4 as sigv4

    _permit_workload(monkeypatch)
    stale = "https://old.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
    assert sigv4._workload_proxy_still_permitted("dashboard:1", upstream_url=stale) is False
    assert sigv4._workload_proxy_still_permitted("dashboard:1", upstream_url=_LIVE_GATEWAY) is True
    assert sigv4._workload_proxy_still_permitted("dashboard:1") is False


def test_proxy_rechecks_permission_after_body_read() -> None:
    """A stalled body must not keep a permit that was revoked before signing."""
    import inspect

    from kiro_crew.platform.agentcore_sigv4 import GatewaySigV4Proxy

    src = inspect.getsource(GatewaySigV4Proxy._handler_class)
    body = src.index("self.rfile.read")
    check = src.rindex("_workload_proxy_still_permitted")
    sign = src.index("sign_aws_request")
    assert body < check < sign
    assert "upstream_url=proxy.upstream_url" in src
