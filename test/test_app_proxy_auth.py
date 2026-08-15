"""Tests for the gateway → app-backend proxy HMAC verifier (CWE-306)."""

import hashlib
import hmac
import time

import pytest

from kiro_crew.apps.proxy_auth import verify_proxy_request

SECRET = "s3cret-app-key"


def _sign(method: str, target: str, body: bytes, *, ts: int | None = None) -> str:
    """Reproduce the gateway's signing (apps/routes.py::handle_app_api_proxy)."""
    ts = int(time.time()) if ts is None else ts
    body_hash = hashlib.sha256(body or b"").hexdigest()
    msg = f"{ts}:{method}:{target}:{body_hash}"
    sig = hmac.new(SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return f"{ts}:{sig}"


def test_valid_signature_passes():
    hdr = _sign("GET", "/api/read?path=x", b"")
    assert verify_proxy_request(hdr, method="GET", target="/api/read?path=x", body=b"", secret=SECRET)


@pytest.mark.parametrize(
    "wire_target",
    [
        "/api/read?path=/tmp/my%20notes.md",
        "/api/read?path=/tmp/my+notes.md",
        "/api/read?path=/tmp/issue%23123.md",
        "/api/read?path=/tmp/caf%C3%A9.md",
        "/api/search?q=hello%20world&dir=/tmp/my%20folder",
    ],
)
def test_wire_form_targets_verify_successfully(wire_target: str):
    """Verify that wire-form targets (containing %20, +, %23, non-ASCII) pass HMAC verification."""
    hdr = _sign("GET", wire_target, b"")
    assert verify_proxy_request(hdr, method="GET", target=wire_target, body=b"", secret=SECRET)


def test_valid_post_binds_body():
    body = b'{"source": "x"}'
    hdr = _sign("POST", "/api/run", body)
    assert verify_proxy_request(hdr, method="POST", target="/api/run", body=body, secret=SECRET)


def test_tampered_body_fails():
    hdr = _sign("POST", "/api/run", b'{"source": "x"}')
    assert not verify_proxy_request(
        hdr, method="POST", target="/api/run", body=b'{"source": "evil"}', secret=SECRET
    )


def test_wrong_target_fails():
    hdr = _sign("GET", "/api/read?path=x", b"")
    assert not verify_proxy_request(hdr, method="GET", target="/api/git-status", body=b"", secret=SECRET)


def test_wrong_method_fails():
    hdr = _sign("GET", "/api/read", b"")
    assert not verify_proxy_request(hdr, method="POST", target="/api/read", body=b"", secret=SECRET)


def test_missing_secret_fails_closed():
    hdr = _sign("GET", "/api/read", b"")
    assert not verify_proxy_request(hdr, method="GET", target="/api/read", body=b"", secret="")


def test_missing_or_malformed_header_fails():
    assert not verify_proxy_request("", method="GET", target="/api/read", body=b"", secret=SECRET)
    assert not verify_proxy_request("no-colon", method="GET", target="/api/read", body=b"", secret=SECRET)
    assert not verify_proxy_request("abc:def", method="GET", target="/api/read", body=b"", secret=SECRET)


def test_stale_timestamp_fails():
    hdr = _sign("GET", "/api/read", b"", ts=int(time.time()) - 120)
    assert not verify_proxy_request(hdr, method="GET", target="/api/read", body=b"", secret=SECRET)


def test_wrong_secret_fails():
    hdr = _sign("GET", "/api/read", b"")
    assert not verify_proxy_request(hdr, method="GET", target="/api/read", body=b"", secret="different")
