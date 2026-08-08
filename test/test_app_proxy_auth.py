"""Tests for the gateway → app-backend proxy HMAC verifier (CWE-306)."""

import hashlib
import hmac
import time

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


# ---------------------------------------------------------------------------
# Regression: percent-encodable characters in query values (issue #2053)
#
# The gateway signs the request-target in its wire form (yarl raw_path_qs).
# The backend verifies against self.path, which is also the wire form.
# Both sides must agree on the encoded representation for paths containing
# spaces, +, #, and non-ASCII characters.
# ---------------------------------------------------------------------------


def test_path_with_space_verifies():
    """A query value with a space, signed and verified in wire form (space → +)."""
    wire_target = "/api/read?path=/tmp/my+notes.md"
    hdr = _sign("GET", wire_target, b"")
    assert verify_proxy_request(
        hdr, method="GET", target=wire_target, body=b"", secret=SECRET
    )


def test_path_with_percent_encoded_space_verifies():
    """A space encoded as %20 (alternative to +) also round-trips."""
    wire_target = "/api/read?path=/tmp/my%20notes.md"
    hdr = _sign("GET", wire_target, b"")
    assert verify_proxy_request(
        hdr, method="GET", target=wire_target, body=b"", secret=SECRET
    )


def test_path_with_hash_verifies():
    """A '#' in a query value is percent-encoded on the wire."""
    wire_target = "/api/read?path=/tmp/issue%23123.md"
    hdr = _sign("GET", wire_target, b"")
    assert verify_proxy_request(
        hdr, method="GET", target=wire_target, body=b"", secret=SECRET
    )


def test_path_with_non_ascii_verifies():
    """Non-ASCII (accented) characters are percent-encoded on the wire."""
    wire_target = "/api/read?path=/tmp/caf%C3%A9.md"
    hdr = _sign("GET", wire_target, b"")
    assert verify_proxy_request(
        hdr, method="GET", target=wire_target, body=b"", secret=SECRET
    )


def test_decoded_vs_wire_form_mismatch_fails():
    """Signing the decoded form while verifying the wire form must fail.

    This is the exact bug: the gateway signed 'my notes.md' (decoded) but the
    backend saw 'my+notes.md' (wire). The two must not verify against each other.
    """
    decoded_target = "/api/read?path=/tmp/my notes.md"
    wire_target = "/api/read?path=/tmp/my+notes.md"
    hdr = _sign("GET", decoded_target, b"")
    assert not verify_proxy_request(
        hdr, method="GET", target=wire_target, body=b"", secret=SECRET
    )
