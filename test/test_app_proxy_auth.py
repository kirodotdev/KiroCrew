"""Tests for the gateway → app-backend proxy HMAC verifier (CWE-306)."""

import hashlib
import hmac
import time

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew.apps.proxy_auth import raw_request_target, verify_proxy_request

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


@pytest.mark.parametrize("codepoint", [0x00E9, 0x63D0, 0x1F600, 0xDCFF])
def test_a_non_ascii_signature_is_a_clean_refusal_not_a_crash(codepoint: int):
    """A non-ASCII signature must be refused like any other wrong one.

    ``hmac.compare_digest`` rejects a ``str`` holding a non-ASCII character by
    raising ``TypeError``. The header is attacker-chosen: any local process can
    open the loopback socket this verifier guards, and aiohttp decodes a header
    byte that is not valid UTF-8 into a lone surrogate, so both shapes arrive
    here. Raising would turn the denial into an unhandled 500, drop the
    connection, and skip the caller's SEL ``proxy_auth_failed`` record -- the
    ASCII-wrong case returns a plain ``False`` and keeps all three. The code
    points are built rather than written as literals because ``0xDCFF`` is a lone
    surrogate, which cannot appear in source.
    """
    ts = int(time.time())
    assert not verify_proxy_request(
        f"{ts}:{chr(codepoint)}",
        method="GET",
        target="/api/read",
        body=b"",
        secret=SECRET,
    )


def test_a_non_ascii_signature_does_not_accept_a_valid_one():
    """Encoding must not collapse distinct credentials to the same bytes."""
    hdr = _sign("GET", "/api/read", b"")
    assert verify_proxy_request(hdr, method="GET", target="/api/read", body=b"", secret=SECRET)
    assert not verify_proxy_request(
        f"{int(time.time())}:{hdr.split(':', 1)[1]}{chr(0x00E9)}",
        method="GET",
        target="/api/read",
        body=b"",
        secret=SECRET,
    )


def test_a_valid_signature_suffixed_with_a_lone_surrogate_is_refused():
    """A surrogate must keep a signature distinct, not be dropped from it.

    This is the case that separates ``surrogatepass`` from ``ignore``. A
    lone surrogate cannot be encoded as UTF-8 at all, so ``ignore`` silently
    DROPS it: a valid signature with one appended encodes to the same bytes as
    the valid signature alone and is accepted. ``surrogatepass`` encodes it,
    so the appended character changes the bytes and the request is refused.

    A non-ASCII character that IS encodable, like the ones the other tests
    use, does not exercise this: ``ignore`` keeps those, so the two encodings
    agree and neither test notices the difference.
    """
    hdr = _sign("GET", "/api/read", b"")
    ts, sig = hdr.split(":", 1)
    assert verify_proxy_request(hdr, method="GET", target="/api/read", body=b"", secret=SECRET)
    assert not verify_proxy_request(
        f"{ts}:{sig}{chr(0xDCFF)}",
        method="GET",
        target="/api/read",
        body=b"",
        secret=SECRET,
    )


def test_wrong_secret_fails():
    hdr = _sign("GET", "/api/read", b"")
    assert not verify_proxy_request(hdr, method="GET", target="/api/read", body=b"", secret="different")


@pytest.mark.parametrize(
    "wire_target",
    [
        "/api/read?path=/tmp/my%20notes.md",
        "/api/read?path=/tmp/caf%C3%A9.md",
        "/api/read?path=/tmp/my+notes.md",
        "/api/search?q=hello%20world&dir=/tmp/my%20folder",
    ],
)
def test_raw_request_target_preserves_wire_encoding(wire_target: str):
    """The aiohttp reconstruction helper must return the raw request-target
    byte-for-byte — the exact string routes.py signs — never a decoded form."""
    req = make_mocked_request("GET", wire_target)
    assert raw_request_target(req) == wire_target


def test_raw_request_target_no_query_appends_nothing():
    """No query string means no '?' on either side of the HMAC."""
    req = make_mocked_request("GET", "/api/vaults")
    assert raw_request_target(req) == "/api/vaults"
