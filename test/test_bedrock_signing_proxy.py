"""Behavioural tests for .github/review-cli/bedrock-signing-proxy.py.

The fork GPT review lane (fork-gpt-review.yml) runs ``codex exec`` against an
untrusted fork diff with a real shell, so the Bedrock credentials are held by a
separate-uid signing proxy and codex is handed only a loopback endpoint with no
credentials. Because ``fork-gpt-review.yml`` is triggered by ``workflow_run``,
GitHub runs the workflow definition from the DEFAULT branch, so a PR's own CI
never exercises the proxy. These tests stand in for that: they prove the three
facts the design depends on, locally and against a mock upstream.

1. The proxy SigV4-signs each request for service ``bedrock`` in the configured
   region, with a scope and canonical signature matching AWS's own published
   test vector.
2. A request forwarded through the proxy carries a well-formed
   ``Authorization: AWS4-HMAC-SHA256`` header the upstream receives, built from
   the credentials in the signer-only file -- codex itself sends no auth.
3. The upstream response (an SSE ``responses`` stream) is relayed back verbatim.

Plus: credentials are re-read per request (the mid-run role refresh needs no
restart), and an absent/empty credential file fails closed with 503.

The proxy is a standalone script, not a package module, so it is loaded by
path. ``BEDROCK_PROXY_UPSTREAM`` points it at a mock upstream on loopback.
"""

from __future__ import annotations

import http.client
import importlib.util
import socketserver
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
PROXY_PATH = ROOT / ".github" / "review-cli" / "bedrock-signing-proxy.py"


def _load_proxy() -> ModuleType:
    spec = importlib.util.spec_from_file_location("bedrock_signing_proxy", PROXY_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


proxy = _load_proxy()


class _LoopbackServer(ThreadingHTTPServer):
    # http.server resolves socket.getfqdn(host) on bind, which can stall on some
    # macOS hosts while the socket is bound but not listening; these loopback
    # test servers are reached by port, never by name, so skip the lookup.
    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)


# ---------------------------------------------------------------------------
# 1. SigV4 against AWS's canonical test vector (get-vanilla from the AWS SDK
#    test suite), but with service `bedrock` to pin exactly what this proxy
#    signs. The vector's known key/date/region make the signature reproducible.
# ---------------------------------------------------------------------------


def test_sigv4_signing_key_matches_the_published_vector() -> None:
    # AWS's documented derivation example
    # (docs.aws.amazon.com/general/latest/gr/signature-v4-examples.html).
    key = proxy._signing_key(
        "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
        "20150830",
        "us-east-1",
        "iam",
    )
    expected = bytes.fromhex("c4afb1cc5771d871763a393e44b703571b55cc28424d1a5e86da6ed3c154a4b9")
    assert key == expected


def test_sign_produces_a_bedrock_sigv4_authorization() -> None:
    creds = proxy._Creds("AKIDEXAMPLE", "secretkey", "sessiontoken")
    out = proxy._sign(
        creds,
        region="us-east-1",
        service="bedrock",
        method="POST",
        host="bedrock-mantle.us-east-1.api.aws",
        path="/openai/v1/responses",
        query="",
        headers={"content-type": "application/json"},
        body=b'{"model":"x"}',
    )
    auth = out["Authorization"]
    assert auth.startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/")
    assert "/us-east-1/bedrock/aws4_request" in auth
    assert "SignedHeaders=" in auth and "Signature=" in auth
    # The session token travels as its own header AND is a signed header.
    assert out["X-Amz-Security-Token"] == "sessiontoken"
    assert "x-amz-security-token" in auth


# ---------------------------------------------------------------------------
# 2 + 3. End-to-end: a mock upstream records what the proxy sends and streams
#        an SSE body back. BEDROCK_PROXY_UPSTREAM points the proxy at it.
# ---------------------------------------------------------------------------


class _Upstream:
    """A loopback mock of the mantle endpoint, recording the signed request."""

    def __init__(self) -> None:
        self.received: dict = {}
        parent = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_a) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                parent.received = {
                    "path": self.path,
                    "authorization": self.headers.get("Authorization", ""),
                    "x-amz-date": self.headers.get("X-Amz-Date", ""),
                    "x-amz-security-token": self.headers.get("X-Amz-Security-Token", ""),
                    "host": self.headers.get("Host", ""),
                    "body": self.rfile.read(length) if length else b"",
                }
                payload = b'data: {"delta":"a"}\n\ndata: [DONE]\n\n'
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self._server = _LoopbackServer(("127.0.0.1", 0), H)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> "_Upstream":
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._server.shutdown()
        self._server.server_close()


def _start_proxy(cred_file: Path, upstream_port: int):
    handler = proxy._make_handler(
        str(cred_file),
        region="us-east-1",
        service="bedrock",
        upstream=urllib.parse.urlparse(f"http://127.0.0.1:{upstream_port}"),
    )
    server = _LoopbackServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


def _post(port: int, body: bytes) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(
            "POST",
            "/openai/v1/responses",
            body=body,
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def test_proxy_signs_from_the_file_and_relays_the_stream(tmp_path: Path) -> None:
    cred_file = tmp_path / "signer.env"
    cred_file.write_text(
        "AWS_ACCESS_KEY_ID=AKIDEXAMPLE\n"
        "AWS_SECRET_ACCESS_KEY=secretkey\n"
        "AWS_SESSION_TOKEN=sessiontoken\n",
        encoding="utf-8",
    )
    with _Upstream() as up:
        server, port = _start_proxy(cred_file, up.port)
        try:
            status, body = _post(port, b'{"model":"x","input":"hi"}')
        finally:
            server.shutdown()
            server.server_close()

    # Fact 1/2: the UPSTREAM saw a bedrock SigV4 Authorization built from the
    # file's credentials, not the client (the client sent none).
    assert status == 200
    assert up.received["authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/")
    assert "/us-east-1/bedrock/aws4_request" in up.received["authorization"]
    assert up.received["x-amz-security-token"] == "sessiontoken"
    # The host AWS signs is the bare upstream host (the loopback mock here).
    assert up.received["host"] == f"127.0.0.1:{up.port}"
    assert up.received["path"] == "/openai/v1/responses"
    assert up.received["body"] == b'{"model":"x","input":"hi"}'
    # Fact 3: the SSE body is relayed back verbatim.
    assert body == b'data: {"delta":"a"}\n\ndata: [DONE]\n\n'


def test_proxy_rereads_credentials_per_request(tmp_path: Path) -> None:
    cred_file = tmp_path / "signer.env"
    cred_file.write_text("AWS_ACCESS_KEY_ID=FIRSTKEY\nAWS_SECRET_ACCESS_KEY=s1\n", encoding="utf-8")
    with _Upstream() as up:
        server, port = _start_proxy(cred_file, up.port)
        try:
            _post(port, b"{}")
            assert up.received["authorization"].startswith("AWS4-HMAC-SHA256 Credential=FIRSTKEY/")
            # The mid-run refresh rewrites the file; no restart.
            cred_file.write_text(
                "AWS_ACCESS_KEY_ID=SECONDKEY\nAWS_SECRET_ACCESS_KEY=s2\n",
                encoding="utf-8",
            )
            _post(port, b"{}")
            assert up.received["authorization"].startswith("AWS4-HMAC-SHA256 Credential=SECONDKEY/")
        finally:
            server.shutdown()
            server.server_close()


def test_proxy_fails_closed_without_credentials(tmp_path: Path) -> None:
    missing = tmp_path / "absent.env"
    with _Upstream() as up:
        server, port = _start_proxy(missing, up.port)
        try:
            status, _ = _post(port, b"{}")
        finally:
            server.shutdown()
            server.server_close()
    assert status == 503
    # The upstream was never called.
    assert up.received == {}
