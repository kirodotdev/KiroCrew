"""Tests for the gateway → app-backend proxy HMAC verifier (CWE-306)."""

import base64
import hashlib
import hmac
import json
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


# ---------------------------------------------------------------------------
# X-KiroCrew-Principal: the opt-in, request-bound principal claim
# ---------------------------------------------------------------------------

_REQUEST_ID = "0123456789abcdef0123456789abcdef"
_BODY = b'{"action":"approve"}'


def _principal(
    *,
    kind: str = "owner-session",
    owner_id: str = "demo-owner",
    method: str = "POST",
    target: str = "/api/write",
    body: bytes = _BODY,
    now: int = 1_000,
    request_id: str = _REQUEST_ID,
) -> str:
    from kiro_crew.apps.proxy_auth import sign_proxy_principal_claim

    return sign_proxy_principal_claim(
        kind=kind,
        owner_id=owner_id,
        method=method,
        target=target,
        body=body,
        secret=SECRET,
        now=now,
        request_id=request_id,
    )


def _verify_principal(
    header: str,
    *,
    method: str = "POST",
    target: str = "/api/write",
    body: bytes = _BODY,
    now: float = 1_000,
    replay_cache=None,
):
    from kiro_crew.apps.proxy_auth import ProxyPrincipalReplayCache, verify_proxy_principal_claim

    return verify_proxy_principal_claim(
        header,
        method=method,
        target=target,
        body=body,
        secret=SECRET,
        now=now,
        replay_cache=replay_cache if replay_cache is not None else ProxyPrincipalReplayCache(),
    )


def _resign(payload: dict, *, method: str = "POST", target: str = "/api/write") -> str:
    """Sign an arbitrary payload with the real secret, as only the gateway could."""
    encoded = (
        base64.urlsafe_b64encode(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        )
        .decode()
        .rstrip("=")
    )
    lines = (
        "kirocrew-proxy-principal-v1",
        str(payload.get("issuedAt")),
        method,
        target,
        hashlib.sha256(_BODY).hexdigest(),
        encoded,
    )
    sig = hmac.new(SECRET.encode(), "\n".join(lines).encode(), hashlib.sha256).hexdigest()
    return f"{encoded}.{sig}"


def _payload(**overrides) -> dict:
    payload = {
        "version": 1,
        "kind": "owner-session",
        "ownerId": "demo-owner",
        "issuedAt": 1_000,
        "requestId": _REQUEST_ID,
    }
    payload.update(overrides)
    return payload


def test_principal_valid_owner_claim_returns_its_fields():
    claim = _verify_principal(_principal())
    assert claim is not None
    assert (claim.kind, claim.owner_id, claim.issued_at, claim.request_id, claim.version) == (
        "owner-session",
        "demo-owner",
        1_000,
        _REQUEST_ID,
        1,
    )


@pytest.mark.parametrize(
    "field, value",
    [
        ("method", "PUT"),
        ("target", "/api/write?force=1"),
        ("body", b'{"action":"merge"}'),
    ],
)
def test_principal_is_bound_to_method_target_and_body(field, value):
    """A claim lifted onto a different request does not verify."""
    assert _verify_principal(_principal(), **{field: value}) is None


def test_principal_tampered_claim_fails_the_signature():
    encoded, sig = _principal().rsplit(".", 1)
    payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    payload["ownerId"] = "other-owner"
    forged = (
        base64.urlsafe_b64encode(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        )
        .decode()
        .rstrip("=")
    )
    assert _verify_principal(f"{forged}.{sig}") is None


def test_principal_wrong_secret_fails():
    from kiro_crew.apps.proxy_auth import ProxyPrincipalReplayCache, verify_proxy_principal_claim

    assert (
        verify_proxy_principal_claim(
            _principal(),
            method="POST",
            target="/api/write",
            body=_BODY,
            secret="another-app-secret",
            now=1_000,
            replay_cache=ProxyPrincipalReplayCache(),
        )
        is None
    )


@pytest.mark.parametrize("verify_at, accepted", [(1_060, True), (1_061, False), (939, False)])
def test_principal_freshness_window_is_sixty_seconds(verify_at, accepted):
    assert (_verify_principal(_principal(now=1_000), now=verify_at) is not None) is accepted


def test_principal_request_id_is_accepted_once():
    from kiro_crew.apps.proxy_auth import ProxyPrincipalReplayCache

    cache = ProxyPrincipalReplayCache()
    header = _principal()
    assert _verify_principal(header, replay_cache=cache) is not None
    assert _verify_principal(header, replay_cache=cache) is None


def test_principal_rejected_claim_takes_no_cache_entry():
    """A forged claim must not burn the request id of the real one."""
    from kiro_crew.apps.proxy_auth import ProxyPrincipalReplayCache

    cache = ProxyPrincipalReplayCache()
    encoded, _sig = _principal().rsplit(".", 1)
    assert _verify_principal(f"{encoded}.{'0' * 64}", replay_cache=cache) is None
    assert _verify_principal(_principal(), replay_cache=cache) is not None


def test_principal_replay_cache_refuses_when_full_instead_of_evicting():
    from kiro_crew.apps.proxy_auth import ProxyPrincipalReplayCache

    cache = ProxyPrincipalReplayCache(max_entries=2)
    assert cache.accept("a" * 32, expires_at=1_060, now=1_000)
    assert cache.accept("b" * 32, expires_at=1_060, now=1_000)
    assert not cache.accept("c" * 32, expires_at=1_060, now=1_000)
    # The live ids are still remembered, so neither is replayable.
    assert not cache.accept("a" * 32, expires_at=1_060, now=1_000)
    # Once their window has closed they are dropped and room returns.
    assert cache.accept("c" * 32, expires_at=1_121, now=1_061)


def test_principal_replay_cache_needs_a_positive_bound():
    from kiro_crew.apps.proxy_auth import ProxyPrincipalReplayCache

    with pytest.raises(ValueError):
        ProxyPrincipalReplayCache(max_entries=0)


@pytest.mark.parametrize(
    "overrides",
    [
        {"version": 2},
        {"kind": "admin"},
        {"issuedAt": True},
        {"issuedAt": 1_000.0},
        {"issuedAt": "1000"},
        {"ownerId": ""},
        {"ownerId": "owner\nname"},
        {"ownerId": "o" * 257},
        {"kind": "app-token"},
        {"kind": "agent-tool"},
        {"kind": "none"},
        {"requestId": _REQUEST_ID.upper()},
        {"requestId": _REQUEST_ID[:-1]},
        {"extra": "field"},
    ],
)
def test_principal_correctly_signed_but_malformed_claim_is_rejected(overrides):
    """Even with a valid HMAC, only the one shape the gateway signs is accepted.

    The ``app-token``/``agent-tool``/``none`` rows keep the owner id the base payload
    carries: only ``owner-session`` may name an owner.
    """
    assert _verify_principal(_resign(_payload(**overrides))) is None


def test_principal_resign_helper_matches_the_gateway():
    """The malformed-claim cases above are only meaningful if ``_resign`` is faithful."""
    assert _verify_principal(_resign(_payload())) is not None


@pytest.mark.parametrize(
    "overrides",
    [
        {"kind": []},
        {"kind": {}},
        {"issuedAt": 10**400},
        {"issuedAt": -(10**400)},
    ],
)
def test_principal_unhashable_kind_or_huge_issue_time_is_a_verdict_not_an_exception(overrides):
    """A local process can send these straight to the backend's loopback port.

    Each value reaches a check that runs before the HMAC compare: an unhashable
    ``kind`` meets the set lookup, and an ``issuedAt`` past float range meets the
    freshness check. Both must return a verdict, or a backend answers 500 instead of
    403. The clock is a float because a real backend's clock is ``time.time()``.
    """
    assert _verify_principal(_resign(_payload(**overrides)), now=1_000.5) is None


@pytest.mark.parametrize(
    "header",
    [
        "",
        "no-period",
        ".deadbeef",
        "abc.",
        "!!!not-base64!!!.deadbeef",
        "x" * 5000 + ".deadbeef",
        "eyJ\udc80.deadbeef",
    ],
)
def test_principal_malformed_header_is_rejected_without_raising(header):
    assert _verify_principal(header) is None


def test_principal_non_ascii_signature_is_a_verdict_not_a_type_error():
    encoded, _sig = _principal().rsplit(".", 1)
    assert _verify_principal(f"{encoded}.é\udc80") is None


def test_principal_missing_secret_fails_closed(monkeypatch):
    from kiro_crew.apps.proxy_auth import ProxyPrincipalReplayCache, verify_proxy_principal_claim

    monkeypatch.delenv("KIROCREW_PROXY_SECRET", raising=False)
    assert (
        verify_proxy_principal_claim(
            _principal(),
            method="POST",
            target="/api/write",
            body=_BODY,
            now=1_000,
            replay_cache=ProxyPrincipalReplayCache(),
        )
        is None
    )


def test_principal_missing_replay_cache_fails_closed():
    from kiro_crew.apps.proxy_auth import verify_proxy_principal_claim

    assert (
        verify_proxy_principal_claim(
            _principal(),
            method="POST",
            target="/api/write",
            body=_BODY,
            secret=SECRET,
            now=1_000,
            replay_cache=None,  # type: ignore[arg-type]
        )
        is None
    )


def test_principal_secret_falls_back_to_the_backend_environment(monkeypatch):
    from kiro_crew.apps.proxy_auth import ProxyPrincipalReplayCache, verify_proxy_principal_claim

    monkeypatch.setenv("KIROCREW_PROXY_SECRET", SECRET)
    claim = verify_proxy_principal_claim(
        _principal(),
        method="POST",
        target="/api/write",
        body=_BODY,
        now=1_000,
        replay_cache=ProxyPrincipalReplayCache(),
    )
    assert claim is not None


@pytest.mark.parametrize(
    "kind, owner_id",
    [("owner-session", ""), ("app-token", "demo-owner"), ("admin", ""), ("none", "x\x00")],
)
def test_principal_signer_refuses_an_invalid_claim(kind, owner_id):
    with pytest.raises(ValueError):
        _principal(kind=kind, owner_id=owner_id)


def test_principal_signer_refuses_an_empty_secret():
    from kiro_crew.apps.proxy_auth import sign_proxy_principal_claim

    with pytest.raises(ValueError):
        sign_proxy_principal_claim(
            kind="none", owner_id="", method="GET", target="/api/x", body=b"", secret=""
        )


def test_principal_signature_comparison_is_constant_time_over_bytes(monkeypatch):
    compared: list[tuple[object, object]] = []
    real = hmac.compare_digest

    def record(left, right):
        compared.append((left, right))
        return real(left, right)

    monkeypatch.setattr("kiro_crew.apps.proxy_auth.hmac.compare_digest", record)
    assert _verify_principal(_principal()) is not None
    assert len(compared) == 1
    assert all(isinstance(value, bytes) for value in compared[0])


def _reference_verify(header: str, *, method: str, target: str, body: bytes, now: int):
    """The documented algorithm, written from the API reference without the module.

    A backend that cannot import ``kiro_crew`` implements exactly these steps, so the
    gateway signer has to stay byte-compatible with them.
    """
    encoded, _, sig = header.rpartition(".")
    payload = json.loads(base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_"))
    assert set(payload) == {"version", "kind", "ownerId", "issuedAt", "requestId"}
    assert payload["version"] == 1
    assert abs(now - payload["issuedAt"]) <= 60
    lines = (
        "kirocrew-proxy-principal-v1",
        str(payload["issuedAt"]),
        method,
        target,
        hashlib.sha256(body).hexdigest(),
        encoded,
    )
    expected = hmac.new(SECRET.encode(), "\n".join(lines).encode(), hashlib.sha256).hexdigest()
    assert hmac.compare_digest(expected.encode(), sig.encode())
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    assert encoded == base64.urlsafe_b64encode(canonical).decode().rstrip("=")
    return payload


def test_principal_gateway_signer_matches_the_documented_algorithm():
    header = _principal(target="/api/write?page=2", request_id="fedcba9876543210" * 2)
    payload = _reference_verify(
        header, method="POST", target="/api/write?page=2", body=_BODY, now=1_000
    )
    assert payload == {
        "issuedAt": 1_000,
        "kind": "owner-session",
        "ownerId": "demo-owner",
        "requestId": "fedcba9876543210" * 2,
        "version": 1,
    }
