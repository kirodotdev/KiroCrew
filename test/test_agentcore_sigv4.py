"""Localhost SigV4 proxy for AgentCore Gateway IAM inbound."""

from __future__ import annotations

import contextlib
import json
import socket
import threading
import time
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
    read_at = handle_src.index("_read_request_body")
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
    monkeypatch.setattr(sigv4, "preferred_bind_port", lambda: 0)
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
    monkeypatch.setattr(sigv4, "preferred_bind_port", lambda: 0)
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
    monkeypatch.setattr(sigv4, "preferred_bind_port", lambda: 0)
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
    monkeypatch.setattr(sigv4, "preferred_bind_port", lambda: 0)
    monkeypatch.setattr(
        sigv4, "_workload_proxy_still_permitted", lambda session_key="", **_k: False
    )
    monkeypatch.setattr(sigv4, "preferred_bind_port", lambda: 0)
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
        "kiro_crew.platform.agentcore_sigv4._session_key_is_live",
        lambda _key: True,
    )
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


def test_proxy_recheck_refuses_torn_down_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """A closed child session must not keep signing on leftover proxy headers."""
    from kiro_crew.platform import agentcore_sigv4 as sigv4
    from kiro_crew.platform.governance_profiles import HOST_SESSION_KEY

    _permit_workload(monkeypatch)
    monkeypatch.setattr(sigv4, "_session_key_is_live", lambda key: key == HOST_SESSION_KEY)
    assert sigv4._workload_proxy_still_permitted("dashboard:1", upstream_url=_LIVE_GATEWAY) is False
    assert (
        sigv4._workload_proxy_still_permitted(HOST_SESSION_KEY, upstream_url=_LIVE_GATEWAY) is True
    )


def test_session_key_is_live_requires_registered_session() -> None:
    from kiro_crew.platform.agentcore_sigv4 import _session_key_is_live
    from kiro_crew.platform.governance_profiles import HOST_SESSION_KEY

    class _Mgr:
        def has_session(self, key: str) -> bool:
            return key == "dashboard:1"

    import kiro_crew.session as session_mod

    mgr = _Mgr()
    session_mod._LIVE_SESSION_MANAGERS.add(mgr)
    try:
        assert _session_key_is_live("dashboard:1") is True
        assert _session_key_is_live("dashboard:closed") is False
        assert _session_key_is_live(HOST_SESSION_KEY) is True
    finally:
        session_mod._LIVE_SESSION_MANAGERS.discard(mgr)


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
    body = src.index("_read_request_body")
    check = src.rindex("_workload_proxy_still_permitted")
    sign = src.index("sign_aws_request")
    assert body < check < sign
    assert "upstream_url=proxy.upstream_url" in src


_GW_URL = "https://demo-gw.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp"


def test_proxy_constructor_rejects_unusable_upstreams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.platform.agentcore_sigv4 import GatewaySigV4Proxy

    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    with pytest.raises(ValueError, match="must be https"):
        GatewaySigV4Proxy("http://127.0.0.1/mcp")
    with pytest.raises(ValueError, match="not a usable URL"):
        GatewaySigV4Proxy("ftp://example.test/mcp", require_https=False)
    with pytest.raises(ValueError, match="must not carry credentials"):
        GatewaySigV4Proxy(
            "https://user:pass@demo-gw.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp"
        )
    with pytest.raises(ValueError, match="needs a region"):
        GatewaySigV4Proxy("https://example.test/mcp")


def test_proxy_start_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.platform import agentcore_sigv4 as sigv4

    monkeypatch.setattr(sigv4, "preferred_bind_port", lambda: 0)
    proxy = sigv4.GatewaySigV4Proxy(
        "http://127.0.0.1:9/mcp",
        region="us-east-1",
        require_https=False,
    )
    first = proxy.start()
    try:
        assert proxy.alive is True
        assert proxy.listen_url == first
        assert proxy.start() == first
    finally:
        proxy.stop()
    assert proxy.alive is False
    assert proxy.listen_url == ""


def test_proxy_rejects_bad_length_and_oversized_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import http.client
    from urllib.parse import urlparse

    from kiro_crew.platform import agentcore_sigv4 as sigv4

    monkeypatch.setattr(sigv4, "preferred_bind_port", lambda: 0)
    monkeypatch.setattr(sigv4, "_workload_proxy_still_permitted", lambda session_key="", **_k: True)
    proxy = sigv4.GatewaySigV4Proxy(
        "http://127.0.0.1:9/mcp",
        region="us-east-1",
        require_https=False,
    )
    listen = proxy.start()
    parsed = urlparse(listen)
    session_key = "agent:main:main"
    headers = {
        sigv4.PROXY_AUTH_HEADER: sigv4.bound_proxy_auth_token(proxy.client_token, session_key),
        sigv4.PROXY_SESSION_HEADER: session_key,
    }
    try:
        bad = http.client.HTTPConnection(parsed.hostname or "127.0.0.1", parsed.port, timeout=5)
        bad.request(
            "POST",
            parsed.path or "/",
            body=b"",
            headers={**headers, "Content-Length": "nope"},
        )
        assert bad.getresponse().status == 400
        bad.close()

        huge = http.client.HTTPConnection(parsed.hostname or "127.0.0.1", parsed.port, timeout=5)
        huge.request(
            "POST",
            parsed.path or "/",
            body=b"",
            headers={**headers, "Content-Length": str(sigv4.PROXY_BODY_MAX_BYTES + 1)},
        )
        assert huge.getresponse().status == 413
        huge.close()
    finally:
        proxy.stop()


def test_ensure_workload_proxy_starts_reuses_and_replaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.platform import agentcore_sigv4 as sigv4

    started: list[str] = []

    class _Fake:
        def __init__(self, url: str) -> None:
            self.upstream_url = url
            self.client_token = "tok"
            self.listen_url = ""
            self._alive = False

        @property
        def alive(self) -> bool:
            return self._alive

        def start(self) -> str:
            self._alive = True
            self.listen_url = "http://127.0.0.1:9/mcp"
            started.append(self.upstream_url)
            return self.listen_url

        def stop(self) -> None:
            self._alive = False

    sigv4.reset_workload_proxy()
    monkeypatch.setattr(sigv4, "GatewaySigV4Proxy", _Fake)
    first = sigv4.ensure_workload_proxy(_GW_URL)
    assert first == "http://127.0.0.1:9/mcp"
    assert started == [_GW_URL]
    assert sigv4.ensure_workload_proxy(_GW_URL) == first
    assert started == [_GW_URL]
    assert sigv4.workload_proxy_auth_token() == "tok"
    headers = sigv4.proxy_auth_headers("dashboard:1", agent="researcher")
    assert headers[sigv4.PROXY_SESSION_HEADER] == "dashboard:1"
    assert headers[sigv4.PROXY_AGENT_HEADER] == "researcher"
    assert sigv4.PROXY_AUTH_HEADER in headers
    assert sigv4.proxy_auth_headers("") == {}
    other = "https://other.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
    assert sigv4.ensure_workload_proxy(other) == first
    assert started == [_GW_URL, other]
    sigv4.reset_workload_proxy()
    assert sigv4.workload_proxy_auth_token() is None
    assert sigv4.proxy_auth_headers("dashboard:1") == {}


def test_ensure_workload_proxy_start_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.platform import agentcore_sigv4 as sigv4

    class _Boom:
        def __init__(self, url: str) -> None:
            self.upstream_url = url

        def start(self) -> str:
            raise OSError("bind failed")

    sigv4.reset_workload_proxy()
    monkeypatch.setattr(sigv4, "GatewaySigV4Proxy", _Boom)
    assert sigv4.ensure_workload_proxy(_GW_URL) is None
    sigv4.reset_workload_proxy()


def test_proxy_recheck_missing_session_and_lookup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.platform import agentcore_sigv4 as sigv4

    assert sigv4._workload_proxy_still_permitted("") is False
    _permit_workload(monkeypatch)

    def _boom() -> None:
        raise RuntimeError("governance down")

    monkeypatch.setattr("kiro_crew.platform.context.current_context", _boom)
    assert sigv4._workload_proxy_still_permitted("dashboard:1", upstream_url=_LIVE_GATEWAY) is False


def test_auth_token_matches_rejects_empty_and_length() -> None:
    from kiro_crew.platform.agentcore_sigv4 import _auth_token_matches

    assert _auth_token_matches("", "abc") is False
    assert _auth_token_matches("ab", "abc") is False
    assert _auth_token_matches("abc", "abc") is True


def test_proxy_401_is_sel_audited(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.platform import agentcore_sigv4 as sigv4

    seen: list[tuple[str, bool, str]] = []

    def _audit(session_key: str, permitted: bool, reason: str = "") -> None:
        seen.append((session_key, permitted, reason))

    monkeypatch.setattr(sigv4, "_audit_proxy_decision", _audit)
    monkeypatch.setattr(sigv4, "preferred_bind_port", lambda: 0)
    proxy = sigv4.GatewaySigV4Proxy(
        "http://127.0.0.1:9/mcp",
        region="us-east-1",
        require_https=False,
    )
    try:
        listen = proxy.start()
        bare = Request(listen, data=b"{}", method="POST")
        with pytest.raises(HTTPError) as denied:
            urlopen(bare, timeout=5)  # noqa: S310  # nosemgrep
        assert denied.value.code == 401
        assert ("", False, "missing_session") in seen
        session_key = "agent:main:main"
        forged = Request(
            listen,
            data=b"{}",
            headers={
                sigv4.PROXY_AUTH_HEADER: "deadbeef",
                sigv4.PROXY_SESSION_HEADER: session_key,
            },
            method="POST",
        )
        with pytest.raises(HTTPError) as forged_denied:
            urlopen(forged, timeout=5)  # noqa: S310  # nosemgrep
        assert forged_denied.value.code == 401
        assert (session_key, False, "proxy_auth") in seen
    finally:
        proxy.stop()


def test_reset_workload_proxy_waits_for_stop() -> None:
    from kiro_crew.platform import agentcore_sigv4 as sigv4

    order: list[str] = []

    class _Rec:
        def stop(self) -> None:
            order.append("stop")

    sigv4.reset_workload_proxy()
    with sigv4._LOCK:
        sigv4._PROXY = _Rec()  # type: ignore[assignment]
    sigv4.reset_workload_proxy()
    order.append("return")
    assert order == ["stop", "return"]
    with sigv4._LOCK:
        assert sigv4._PROXY is None


def test_proxy_stop_waits_for_in_flight_handler_slots() -> None:
    from kiro_crew.platform.agentcore_sigv4 import (
        PROXY_MAX_INFLIGHT,
        GatewaySigV4Proxy,
    )

    slots = threading.BoundedSemaphore(PROXY_MAX_INFLIGHT)
    assert slots.acquire(blocking=False)
    proxy = GatewaySigV4Proxy("https://abc.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp")
    proxy._handler_slots = slots
    order: list[str] = []

    def _release() -> None:
        time.sleep(0.05)
        order.append("release")
        slots.release()

    thread = Thread(target=_release)
    thread.start()
    proxy.stop()
    order.append("stopped")
    thread.join()
    assert order == ["release", "stopped"]


def test_proxy_stop_acquires_every_slot_without_deadline() -> None:
    from kiro_crew.platform.agentcore_sigv4 import (
        PROXY_MAX_INFLIGHT,
        GatewaySigV4Proxy,
    )

    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    class _Slots:
        def acquire(self, *args: Any, **kwargs: Any) -> bool:
            calls.append((args, kwargs))
            return True

    proxy = GatewaySigV4Proxy("https://abc.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp")
    proxy._handler_slots = _Slots()  # type: ignore[assignment]
    proxy.stop()
    assert len(calls) == PROXY_MAX_INFLIGHT
    assert all(args == () and kwargs == {} for args, kwargs in calls)


def test_proxy_stop_closes_unauthed_sockets() -> None:
    from kiro_crew.platform.agentcore_sigv4 import GatewaySigV4Proxy

    closed: list[object] = []

    class _Sock:
        def close(self) -> None:
            closed.append(self)

    sock = _Sock()
    proxy = GatewaySigV4Proxy("https://abc.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp")
    proxy._unauthed_requests.add(sock)
    proxy.stop()
    assert closed == [sock]
    assert proxy._unauthed_requests == set()


def test_read_request_body_aborts_immediately_when_stopping() -> None:
    from kiro_crew.platform.agentcore_sigv4 import _read_request_body

    class _RFile:
        def read(self, n: int) -> bytes:
            time.sleep(30)
            return b"x" * n

    class _Sock:
        def gettimeout(self) -> float:
            return 300.0

        def settimeout(self, _t: float) -> None:
            return None

    started = time.monotonic()
    with pytest.raises(OSError, match="stopping"):
        _read_request_body(_RFile(), _Sock(), 64, stopping=lambda: True)
    assert time.monotonic() - started < 1.0


def test_read_request_body_retries_after_idle_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.platform import agentcore_sigv4 as sigv4

    reads = {"n": 0}
    selects = {"n": 0}

    class _Sock:
        def __init__(self) -> None:
            self.timeout = 300.0
            self.timeouts: list[float] = []

        def fileno(self) -> int:
            return 7

        def gettimeout(self) -> float:
            return self.timeout

        def settimeout(self, value: float) -> None:
            self.timeouts.append(value)
            self.timeout = value

    class _RFile:
        def __init__(self, sock: _Sock) -> None:
            self._sock = sock

        def read1(self, n: int) -> bytes:
            reads["n"] += 1
            if self._sock.timeout == 0:
                raise BlockingIOError()
            return b"abc"

    def _select(
        _r: object, _w: object, _x: object, _timeout: float
    ) -> tuple[list[object], list[object], list[object]]:
        selects["n"] += 1
        if selects["n"] == 1:
            return [], [], []
        return [object()], [], []

    monkeypatch.setattr(sigv4.select, "select", _select)
    sock = _Sock()
    body = sigv4._read_request_body(_RFile(sock), sock, 3, stopping=lambda: False)
    assert body == b"abc"
    assert reads["n"] == 3
    assert selects["n"] >= 2
    assert sigv4.PROXY_BODY_IDLE_SECS not in sock.timeouts


def test_read_request_body_silent_stall_hits_socket_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.platform import agentcore_sigv4 as sigv4

    monkeypatch.setattr(sigv4, "PROXY_SOCKET_TIMEOUT_SECS", 0.0)
    monkeypatch.setattr(sigv4.select, "select", lambda *_a: ([], [], []))

    class _Sock:
        def __init__(self) -> None:
            self.timeout = 300.0

        def fileno(self) -> int:
            return 7

        def gettimeout(self) -> float:
            return self.timeout

        def settimeout(self, value: float) -> None:
            self.timeout = value

    class _RFile:
        def __init__(self, sock: _Sock) -> None:
            self._sock = sock

        def read1(self, n: int) -> bytes:
            if self._sock.timeout == 0:
                raise BlockingIOError()
            raise AssertionError("blocking read must not run on a silent stall")

    sock = _Sock()
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="idle timeout"):
        sigv4._read_request_body(_RFile(sock), sock, 64, stopping=lambda: False)
    assert time.monotonic() - started < 1.0


def test_forward_stream_silent_stall_hits_socket_timeout() -> None:
    import inspect

    from kiro_crew.platform import agentcore_sigv4 as sigv4

    src = inspect.getsource(sigv4._read_request_body)
    forward = inspect.getsource(sigv4.GatewaySigV4Proxy._forward)
    assert "PROXY_SOCKET_TIMEOUT_SECS" in src
    assert "PROXY_SOCKET_TIMEOUT_SECS" in forward
    assert "except TimeoutError:" not in src
    assert "except TimeoutError:" not in forward


def test_read_request_body_does_not_timeout_buffered_reader() -> None:
    import inspect

    from kiro_crew.platform import agentcore_sigv4 as sigv4

    src = inspect.getsource(sigv4._read_request_body)
    forward = inspect.getsource(sigv4.GatewaySigV4Proxy._forward)
    assert "except TimeoutError:" not in src
    assert "except TimeoutError:" not in forward
    assert "_socket_readable" in src
    assert "_socket_readable" in forward


def test_proxy_stop_closes_authenticated_incomplete_upload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Valid proxy headers + incomplete body must not stall Save → Off."""
    from kiro_crew.platform import agentcore_sigv4 as sigv4

    monkeypatch.setattr(sigv4, "preferred_bind_port", lambda: 0)
    monkeypatch.setattr(sigv4, "_workload_proxy_still_permitted", lambda session_key="", **_k: True)
    proxy = sigv4.GatewaySigV4Proxy(
        "http://127.0.0.1:9/mcp",
        region="us-east-1",
        require_https=False,
    )
    sock: socket.socket | None = None
    try:
        listen = proxy.start()
        port = int(listen.rsplit(":", 1)[1].split("/", 1)[0])
        session_key = "agent:main:main"
        token = sigv4.bound_proxy_auth_token(proxy.client_token, session_key)
        sock = socket.create_connection(("127.0.0.1", port), timeout=2)
        sock.sendall(
            (
                "POST /mcp HTTP/1.1\r\n"
                "Host: 127.0.0.1\r\n"
                f"{sigv4.PROXY_AUTH_HEADER}: {token}\r\n"
                f"{sigv4.PROXY_SESSION_HEADER}: {session_key}\r\n"
                "Content-Length: 64\r\n"
                "\r\n"
            ).encode()
        )
        deadline = time.time() + 2.0
        while time.time() < deadline and not proxy._unauthed_requests:
            time.sleep(0.01)
        assert proxy._unauthed_requests
        started = time.monotonic()
        proxy.stop()
        assert time.monotonic() - started < 5.0
        assert proxy._unauthed_requests == set()
    finally:
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.close()
        if proxy.alive:
            proxy.stop()


def test_proxy_keeps_authorized_socket_closeable() -> None:
    """Save → Off must still close a hop that already passed permit."""
    import inspect

    from kiro_crew.platform.agentcore_sigv4 import GatewaySigV4Proxy

    src = inspect.getsource(GatewaySigV4Proxy._handler_class)
    check = src.rindex("_workload_proxy_still_permitted")
    assert "unauthed_requests.discard(self.connection)" not in src[check:]


def test_proxy_stop_closes_upstream_connections() -> None:
    from kiro_crew.platform.agentcore_sigv4 import GatewaySigV4Proxy

    closed: list[object] = []

    class _Conn:
        def close(self) -> None:
            closed.append(self)

    conn = _Conn()
    proxy = GatewaySigV4Proxy("https://abc.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp")
    proxy._upstream_conns.add(conn)
    proxy.stop()
    assert closed == [conn]
    assert proxy._upstream_conns == set()


def test_proxy_stop_closes_authorized_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An authorized hop must stay closeable so Save → Off cannot stall."""
    from kiro_crew.platform import agentcore_sigv4 as sigv4

    monkeypatch.setattr(sigv4, "sign_aws_request", lambda **_k: {"Authorization": "t"})
    monkeypatch.setattr(sigv4, "_workload_proxy_still_permitted", lambda session_key="", **_k: True)
    monkeypatch.setattr(sigv4, "preferred_bind_port", lambda: 0)
    proxy = sigv4.GatewaySigV4Proxy(
        "http://127.0.0.1:9/mcp",
        region="us-east-1",
        require_https=False,
    )
    holding = threading.Event()

    def _hold_forward(
        handler: BaseHTTPRequestHandler,
        method: str,
        target: str,
        headers: Any,
        body: bytes,
    ) -> None:
        holding.set()
        deadline = time.time() + 30
        while not proxy._stopping and time.time() < deadline:
            time.sleep(0.05)

    monkeypatch.setattr(proxy, "_forward", _hold_forward)
    client: Thread | None = None
    try:
        listen = proxy.start()
        session_key = "agent:main:main"

        def _hold() -> None:
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
            with contextlib.suppress(Exception):
                urlopen(req, timeout=30)  # noqa: S310  # nosemgrep

        client = Thread(target=_hold, daemon=True)
        client.start()
        assert holding.wait(5.0)
        assert proxy._unauthed_requests
        started = time.monotonic()
        proxy.stop()
        assert time.monotonic() - started < 5.0
        assert proxy._unauthed_requests == set()
    finally:
        if client is not None:
            client.join(timeout=2.0)
        if proxy.alive:
            proxy.stop()


def test_sign_aws_request_requires_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.platform import agentcore_sigv4 as sigv4

    class _Session:
        def get_credentials(self) -> None:
            return None

    monkeypatch.setattr("botocore.session.Session", _Session)
    with pytest.raises(RuntimeError, match="no AWS credentials"):
        sigv4.sign_aws_request(
            method="POST",
            url="https://example.test/mcp",
            headers={},
            body=b"{}",
            region="us-west-2",
        )

    class _Frozen:
        access_key = "AKIATEST"
        secret_key = "secret"
        token = None

    class _Creds:
        def get_frozen_credentials(self) -> _Frozen:
            return _Frozen()

    headers = sigv4.sign_aws_request(
        method="POST",
        url="https://example.test/mcp",
        headers={"Accept": "application/json"},
        body=b"{}",
        region="us-west-2",
        credentials=_Creds(),
    )
    assert "Authorization" in headers


def test_filter_incoming_headers_drops_hop_by_hop() -> None:
    from kiro_crew.platform.agentcore_sigv4 import _filter_incoming_headers

    out = _filter_incoming_headers(
        {
            "Accept": "application/json",
            "Connection": "close",
            "Keep-Alive": "timeout=5",
            "X-Custom": "keep",
        }
    )
    assert out == {"Accept": "application/json", "X-Custom": "keep"}


def test_proxy_forward_connects_before_register() -> None:
    """stop() must not leave a window where request() opens a new upstream."""
    import inspect

    from kiro_crew.platform.agentcore_sigv4 import GatewaySigV4Proxy

    src = inspect.getsource(GatewaySigV4Proxy._forward)
    assert src.index("conn.connect()") < src.index("_upstream_conns.add(conn)")
    assert src.index("if self._stopping") < src.index("_upstream_conns.add(conn)")
    assert src.index("_upstream_conns.add(conn)") < src.index("conn.request(")


def test_proxy_forward_aborts_after_connect_when_stopping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import http.client

    from kiro_crew.platform.agentcore_sigv4 import GatewaySigV4Proxy

    order: list[str] = []

    class _Conn:
        def __init__(self, *args: object, **kwargs: object) -> None:
            return

        def connect(self) -> None:
            order.append("connect")

        def request(self, *args: object, **kwargs: object) -> None:
            order.append("request")

        def close(self) -> None:
            order.append("close")

        sock = None

    class _Handler:
        def send_error(self, code: int, message: str = "") -> None:
            order.append(f"error-{code}")

    monkeypatch.setattr(http.client, "HTTPConnection", _Conn)
    proxy = GatewaySigV4Proxy("http://127.0.0.1:9/mcp", region="us-west-2", require_https=False)
    proxy._stopping = True
    proxy._forward(_Handler(), "POST", "http://127.0.0.1:9/mcp", {}, b"")
    assert order == ["connect", "close", "error-503"]
    assert proxy._upstream_conns == set()
