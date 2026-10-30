"""Tests for the replay-proof v2 proxy signature and its nonce cache."""

from __future__ import annotations

import hashlib
import hmac

import pytest

from kiro_crew.apps.proxy_auth import (
    ProxyPrincipalReplayCache,
    new_proxy_nonce,
    sign_proxy_request_v2,
    verify_proxy_request,
    verify_proxy_request_v2,
)

SECRET = "s3cret-app-key"
NONCE = "0123456789abcdef" * 2
NOW = 1_000_000
TARGET = "/api/write?page=2&q=a%20b"
BODY = b'{"text":"hi"}'


def _sign(**overrides) -> str:
    args = {
        "secret": SECRET,
        "ts": NOW,
        "nonce": NONCE,
        "method": "POST",
        "target": TARGET,
        "body": BODY,
    }
    args.update(overrides)
    return sign_proxy_request_v2(**args)


def _verify(header: str, nonce: str = NONCE, **overrides) -> bool:
    args = {
        "method": "POST",
        "target": TARGET,
        "body": BODY,
        "replay_cache": ProxyPrincipalReplayCache(),
        "secret": SECRET,
        "now": NOW,
    }
    args.update(overrides)
    return verify_proxy_request_v2(header, nonce, **args)


def test_signature_matches_the_documented_algorithm():
    ts, _, sig = _sign().partition(":")
    body_hash = hashlib.sha256(BODY).hexdigest()
    message = f"v2:{NOW}:{NONCE}:POST:{TARGET}:{body_hash}"
    assert ts == str(NOW)
    assert sig == hmac.new(SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()


def test_valid_signature_is_accepted():
    assert _verify(_sign())


def test_an_identical_replay_is_refused():
    cache = ProxyPrincipalReplayCache()
    header = _sign()
    assert _verify(header, replay_cache=cache)
    assert not _verify(header, replay_cache=cache)
    assert not _verify(header, replay_cache=cache, now=NOW + 59)


def test_a_fresh_nonce_on_the_same_request_is_accepted():
    cache = ProxyPrincipalReplayCache()
    other = "f" * 32
    assert _verify(_sign(), replay_cache=cache)
    assert _verify(_sign(nonce=other), other, replay_cache=cache)


@pytest.mark.parametrize(
    ("verify_at", "accepted"),
    [(NOW - 60, True), (NOW + 60, True), (NOW - 61, False), (NOW + 61, False)],
)
def test_freshness_window_is_sixty_seconds_each_way(verify_at, accepted):
    assert _verify(_sign(), now=verify_at) is accepted


@pytest.mark.parametrize(
    "overrides",
    [
        {"method": "PUT"},
        {"target": "/api/write?page=3&q=a%20b"},
        {"body": b'{"text":"bye"}'},
        {"secret": "other-secret"},
    ],
)
def test_a_tampered_field_fails_the_signature(overrides):
    assert not _verify(_sign(), **overrides)


def test_a_swapped_nonce_fails_the_signature():
    assert not _verify(_sign(), "a" * 32)


def test_a_rejected_request_takes_no_cache_entry():
    cache = ProxyPrincipalReplayCache()
    assert not _verify(_sign(), method="PUT", replay_cache=cache)
    assert _verify(_sign(), replay_cache=cache)


def test_a_full_cache_refuses_instead_of_evicting():
    cache = ProxyPrincipalReplayCache(max_entries=1)
    other = "f" * 32
    assert _verify(_sign(), replay_cache=cache)
    assert not _verify(_sign(nonce=other), other, replay_cache=cache)
    # The live nonce is still held, so it is not replayable either.
    assert not _verify(_sign(), replay_cache=cache)
    # Once its window closes, room returns.
    assert _verify(_sign(ts=NOW + 61, nonce=other), other, replay_cache=cache, now=NOW + 61)


@pytest.mark.parametrize(
    "header",
    [
        "",
        "no-colon",
        f"{NOW}:",
        ":abc",
        f"x{NOW}:abc",
        f"-{NOW}:abc",
        "\u00b2:abc",
        "9" * 13 + ":abc",
        "9" * 5000 + ":abc",
    ],
)
def test_a_malformed_header_is_a_verdict_not_an_exception(header):
    assert not _verify(header)


@pytest.mark.parametrize(
    "nonce",
    ["", "short", NONCE.upper(), NONCE + "0", NONCE[:-1] + "g", NONCE[:-1] + "\udcff"],
)
def test_a_malformed_nonce_is_refused(nonce):
    assert not _verify(_sign(), nonce)


def test_a_non_ascii_signature_is_a_verdict_not_a_type_error():
    ts, _, sig = _sign().partition(":")
    assert not _verify(f"{ts}:{sig[:-1]}\u00e9")
    assert not _verify(f"{ts}:{sig}\udcff")


def test_missing_secret_or_cache_fails_closed(monkeypatch):
    monkeypatch.delenv("KIROCREW_PROXY_SECRET", raising=False)
    assert not _verify(_sign(), secret=None)
    assert not _verify(_sign(), secret="")
    assert not _verify(_sign(), replay_cache=None)


def test_secret_falls_back_to_the_backend_environment(monkeypatch):
    monkeypatch.setenv("KIROCREW_PROXY_SECRET", SECRET)
    assert _verify(_sign(), secret=None)


@pytest.mark.parametrize(
    "overrides", [{"secret": ""}, {"ts": -1}, {"nonce": "short"}, {"nonce": NONCE.upper()}]
)
def test_signer_refuses_input_no_verifier_would_accept(overrides):
    with pytest.raises(ValueError):
        _sign(**overrides)


def test_a_v2_signature_is_not_a_valid_v1_signature():
    assert not verify_proxy_request(
        _sign(), method="POST", target=TARGET, body=BODY, secret=SECRET, now=NOW
    )


def test_new_nonces_have_the_signed_shape_and_differ():
    first, second = new_proxy_nonce(), new_proxy_nonce()
    assert first != second
    for nonce in (first, second):
        assert len(nonce) == 32 and set(nonce) <= set("0123456789abcdef")
