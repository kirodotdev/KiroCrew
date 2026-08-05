"""Tests for token_auth prompt + signed-claim support.

The per-message Slack challenge-and-redirect feature has been removed, but
``token_auth`` retains generic prompt-embedding and signed extra-claim support
(used by user-invoked dashboard links). These tests cover that token behavior.
"""

from __future__ import annotations

import pytest

from kiro_crew.dashboard.token_auth import (
    extract_claims_from_token,
    extract_prompt_from_token,
    generate_token,
    revoke_all_sessions,
    validate_token,
)


@pytest.fixture(autouse=True)
def clear_nonces():
    revoke_all_sessions()
    yield
    revoke_all_sessions()


# -- Token with prompt --


class TestTokenWithPrompt:
    """Token generation and validation with embedded prompt."""

    def test_generate_token_includes_prompt_in_payload(self):
        token = generate_token("user1", 3600, prompt="hello world")
        prompt = extract_prompt_from_token(token)
        assert prompt == "hello world"

    def test_generate_token_without_prompt_returns_empty(self):
        token = generate_token("user1", 3600)
        prompt = extract_prompt_from_token(token)
        assert prompt == ""

    def test_prompt_covered_by_hmac_signature(self):
        """Tampering with the prompt in the payload invalidates the signature."""
        import base64
        import json

        token = generate_token("user1", 3600, prompt="original")
        encoded_payload, sig = token.split(".", 1)

        # Decode, tamper, re-encode
        padding = 4 - len(encoded_payload) % 4
        payload_bytes = base64.urlsafe_b64decode(encoded_payload + "=" * (padding % 4))
        data = json.loads(payload_bytes)
        data["prompt"] = "tampered"
        tampered_payload = (
            base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode())
            .rstrip(b"=")
            .decode()
        )

        tampered_token = f"{tampered_payload}.{sig}"
        valid, _, reason = validate_token(tampered_token)
        assert valid is False
        assert reason == "invalid signature"

    def test_token_with_prompt_validates_normally(self):
        token = generate_token("user1", 3600, prompt="test prompt")
        valid, user_id, reason = validate_token(token)
        assert valid is True
        assert user_id == "user1"
        assert reason == ""

    def test_prompt_with_special_characters(self):
        prompt = "What's the status of @user's deployment? 🚀 <script>alert(1)</script>"
        token = generate_token("user1", 3600, prompt=prompt)
        extracted = extract_prompt_from_token(token)
        assert extracted == prompt


# -- Thread context in token (reconnect / auto-link) --


class TestTokenSlackContext:
    """Token carries channel/thread_ts/session_key signed claims."""

    def test_extra_claims_signed_and_extractable(self):
        token = generate_token(
            "U1",
            3600,
            prompt="hi",
            extra={"channel": "C9", "thread_ts": "1700.5", "session_key": "dashboard:chat-1-9"},
        )
        claims = extract_claims_from_token(token, ("channel", "thread_ts", "session_key"))
        assert claims == {
            "channel": "C9",
            "thread_ts": "1700.5",
            "session_key": "dashboard:chat-1-9",
        }

    def test_extra_cannot_override_reserved_claims(self):
        token = generate_token("U1", 3600, extra={"sub": "evil", "nonce": "x", "channel": "C9"})
        valid, user_id, _ = validate_token(token)
        assert valid is True
        assert user_id == "U1"  # sub not overridden
        assert extract_claims_from_token(token, ("channel",)) == {"channel": "C9"}

    def test_claims_empty_when_token_tampered(self):
        token = generate_token("U1", 3600, extra={"channel": "C9"})
        encoded, sig = token.split(".", 1)
        tampered = f"{encoded}.{'A' * len(sig)}"
        assert extract_claims_from_token(tampered, ("channel",)) == {}

    def test_claims_absent_returns_empty(self):
        token = generate_token("U1", 3600, prompt="hi")
        assert extract_claims_from_token(token, ("channel", "thread_ts", "session_key")) == {}


class TestExtractClaimsAfterLinkWindow:
    """extract_claims_from_token must survive past the 5-min link window."""

    def test_claims_recoverable_after_link_exp(self):
        import time as _time
        from unittest.mock import patch

        # Mint a challenge token (link exp = now+5min, session_exp = now+1h).
        with patch("kiro_crew.dashboard.token_auth.time") as mock_time:
            mock_time.time.return_value = 1000.0
            token = generate_token("U1", 3600, extra={"channel": "C9", "thread_ts": "1700.5"})
        # Advance past the 5-min link window but within the 1h session.
        with patch("kiro_crew.dashboard.token_auth.time") as mock_time:
            mock_time.time.return_value = 1000.0 + 301
            claims = extract_claims_from_token(token, ("channel", "thread_ts"))
        # Validated against session_exp, so the thread context is still recoverable.
        assert claims == {"channel": "C9", "thread_ts": "1700.5"}
        assert _time  # keep import referenced
