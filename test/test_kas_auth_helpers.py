"""Synchronous tests for the loopback portal's PKCE / URL helpers.

Kept separate from test_kas_auth_flows.py so these sync tests do not sit under that
file's module-level ``pytestmark = pytest.mark.asyncio``.
"""

from __future__ import annotations

from kiro_crew.auth.login.endpoints import CALLBACK_PORTS
from kiro_crew.auth.login.portal import (
    build_auth_url,
    generate_code_challenge,
    generate_code_verifier,
    generate_state,
    rebuild_redirect_uri,
)


def test_pkce_verifier_length_and_charset():
    v = generate_code_verifier()
    assert 43 <= len(v) <= 128
    assert "=" not in v  # url-safe, unpadded


def test_pkce_challenge_is_deterministic_s256():
    v = generate_code_verifier()
    assert generate_code_challenge(v) == generate_code_challenge(v)
    assert "=" not in generate_code_challenge(v)


def test_state_is_random_alphanumeric():
    s1, s2 = generate_state(), generate_state()
    assert len(s1) == 10 and s1.isalnum()
    assert s1 != s2  # overwhelmingly likely


def test_build_auth_url_shape(monkeypatch):
    monkeypatch.delenv("KIRO_AUTH_PORTAL_URL", raising=False)
    url = build_auth_url(3128, "STATE", "CHALLENGE")
    assert url.startswith("https://app.kiro.dev/signin?")
    assert "state=STATE" in url
    assert "code_challenge=CHALLENGE" in url
    assert "code_challenge_method=S256" in url
    assert "redirect_uri=http%3A%2F%2Flocalhost%3A3128" in url
    assert "redirect_from=kirocli" in url


def test_build_auth_url_honors_portal_override(monkeypatch):
    monkeypatch.setenv("KIRO_AUTH_PORTAL_URL", "https://staging.example.test/")
    url = build_auth_url(4649, "S", "C")
    assert url.startswith("https://staging.example.test/signin?")


def test_rebuild_redirect_uri_matches_kiro_cli_shape():
    uri = rebuild_redirect_uri(3128, "/oauth/callback", "google")
    assert uri == "http://localhost:3128/oauth/callback?login_option=google"


def test_all_callback_ports_are_valid_range():
    for port in CALLBACK_PORTS:
        assert 1 <= port <= 65535
    assert len(set(CALLBACK_PORTS)) == len(CALLBACK_PORTS)  # no dupes
