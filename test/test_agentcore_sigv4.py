"""Localhost SigV4 proxy for AgentCore Gateway IAM inbound."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any
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


def test_preferred_port_server_reuses_address() -> None:
    from kiro_crew.platform.agentcore_sigv4 import GatewaySigV4Proxy

    proxy = GatewaySigV4Proxy(
        "http://127.0.0.1:9/mcp",
        region="us-east-1",
        require_https=False,
    )
    try:
        proxy.start()
        httpd = proxy._httpd
        assert httpd is not None
        assert httpd.allow_reuse_address is True
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
    proxy = sigv4.GatewaySigV4Proxy(upstream_url, region="us-east-1", require_https=False)
    try:
        listen = proxy.start()
        assert listen.startswith("http://127.0.0.1:")
        req = Request(
            listen,
            data=b'{"jsonrpc":"2.0"}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(
            req, timeout=5
        ) as resp:  # noqa: S310 — loopback test  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
            body = json.loads(resp.read().decode())
        assert body == {"ok": True}
        assert seen["path"] == "/mcp"
        assert seen["body"] == b'{"jsonrpc":"2.0"}'
        assert seen["x-test-signed"] == "1"
        assert seen["authorization"] == "AWS4-HMAC-SHA256 Credential=test"
    finally:
        proxy.stop()
        upstream.shutdown()
        upstream.server_close()


def test_ensure_workload_proxy_refuses_non_https() -> None:
    from kiro_crew.platform.agentcore_sigv4 import ensure_workload_proxy, reset_workload_proxy

    reset_workload_proxy()
    assert ensure_workload_proxy("http://127.0.0.1/mcp") is None
    reset_workload_proxy()
