"""Tests for kiro_crew.metrics.schema — tel-01 foundation contracts."""

import base64

import pytest

from kiro_crew.metrics.schema import (
    MAX_ATTR_COUNT,
    MAX_ATTR_VALUE_LEN,
    redact,
    validate_attrs,
    validate_name,
)

# ---------------------------------------------------------------------------
# C4 — Namespace validation: app cannot spoof kirocrew.*
# ---------------------------------------------------------------------------


class TestValidateName:
    """C4: app.<id>.* namespace enforcement."""

    def test_app_cannot_spoof_kirocrew_namespace(self):
        """Apps MUST NOT be able to emit metrics under kirocrew.*"""
        with pytest.raises(ValueError, match="cannot emit.*kirocrew"):
            validate_name("kirocrew.session.count", app_id="my_app")

    def test_app_cannot_spoof_gen_ai_namespace(self):
        """Apps MUST NOT be able to emit metrics under gen_ai.*"""
        with pytest.raises(ValueError, match="cannot emit.*gen_ai"):
            validate_name("gen_ai.token.usage", app_id="my_app")

    def test_app_must_use_own_prefix(self):
        """App metrics must start with app.<app_id>.*"""
        with pytest.raises(ValueError, match="must start with"):
            validate_name("app.other_app.metric", app_id="my_app")

    def test_app_valid_name(self):
        """Valid app metric name passes."""
        result = validate_name("app.my_app.requests", app_id="my_app")
        assert result == "app.my_app.requests"

    def test_core_valid_kirocrew_name(self):
        """Core caller can use kirocrew.* namespace."""
        result = validate_name("kirocrew.session.duration")
        assert result == "kirocrew.session.duration"

    def test_core_valid_gen_ai_name(self):
        """Core caller can use gen_ai.* namespace."""
        result = validate_name("gen_ai.client.token.usage")
        assert result == "gen_ai.client.token.usage"

    def test_invalid_name_format(self):
        """Reject names that don't match dotted identifier pattern."""
        with pytest.raises(ValueError, match="Invalid metric name"):
            validate_name("INVALID-NAME!")

    def test_empty_name_rejected(self):
        with pytest.raises(ValueError):
            validate_name("")


# ---------------------------------------------------------------------------
# C4 — Attribute validation: credential redaction + cardinality cap
# ---------------------------------------------------------------------------


class TestValidateAttrs:
    """C4: validate_attrs redacts secrets and caps count."""

    def test_credential_shaped_value_redacted(self):
        """High-entropy / credential-shaped values MUST be redacted."""
        attrs = {
            "api_key": "AKIAIOSFODNN7EXAMPLE1",  # AWS key pattern
            "safe_enum": "dashboard",
        }
        result = validate_attrs(attrs)
        assert result["api_key"] == "[REDACTED]"
        assert result["safe_enum"] == "dashboard"

    def test_jwt_like_value_redacted(self):
        """JWT-shaped values are redacted."""
        jwt = (
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0In0"
            ".dBjftJeZ4CVP_mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        )
        attrs = {"token": jwt}
        result = validate_attrs(attrs)
        assert result["token"] == "[REDACTED]"

    def test_safe_values_pass_through(self):
        """Low-cardinality safe values are unchanged."""
        attrs = {
            "session_source": "slack",
            "model": "claude-opus-4.6",
            "success": True,
            "count": 42,
            "latency_ms": 123.45,
        }
        result = validate_attrs(attrs)
        assert result == attrs

    def test_attr_count_capped(self):
        """Attribute count exceeding MAX_ATTR_COUNT raises ValueError."""
        attrs = {f"key_{i}": f"v{i}" for i in range(MAX_ATTR_COUNT + 1)}
        with pytest.raises(ValueError, match="exceeds maximum"):
            validate_attrs(attrs)

    def test_exactly_max_attrs_allowed(self):
        """Exactly MAX_ATTR_COUNT attributes are allowed."""
        attrs = {f"key_{i}": f"v{i}" for i in range(MAX_ATTR_COUNT)}
        result = validate_attrs(attrs)
        assert len(result) == MAX_ATTR_COUNT

    def test_numeric_attrs_not_redacted(self):
        """Numeric and bool attributes pass through without redaction."""
        attrs = {"count": 9999999, "ratio": 0.123456789, "flag": False}
        result = validate_attrs(attrs)
        assert result == attrs


# ---------------------------------------------------------------------------
# Privacy: redact() helper
# ---------------------------------------------------------------------------


class TestRedact:
    """Verify redact() catches known dangerous patterns."""

    def test_aws_key_redacted(self):
        assert redact("AKIAIOSFODNN7EXAMPLE1") == "[REDACTED]"

    def test_secret_access_key_pattern_redacted(self):
        """SecretAccessKey= pattern is caught (security-controls fix)."""
        assert redact("SecretAccessKey=wJalrXUtnFEMI/K7MDENG") == "[REDACTED]"

    def test_private_key_header_redacted(self):
        """Private key headers are caught (security-controls fix)."""
        assert redact("-----BEGIN RSA PRIVATE KEY-----") == "[REDACTED]"

    def test_long_hex_40_plus_redacted(self):
        """40+ hex chars are redacted (raised from 20 to spare trace ids)."""
        assert redact("a" * 40 + "b" * 10) == "[REDACTED]"

    def test_short_hex_32_chars_not_redacted(self):
        """32-char hex (trace IDs, UUIDs) should NOT be redacted."""
        trace_id = "abcdef0123456789abcdef0123456789"  # 32 chars
        assert redact(trace_id) != "[REDACTED]"

    def test_long_non_suspicious_string_truncated(self):
        """Long but low-entropy strings are truncated, not redacted."""
        # Repeating non-hex pattern = low entropy, not a secret, not hex
        long_str = "hello_world_" * 15  # 180 chars, very low entropy
        result = redact(long_str)
        assert result == long_str[:MAX_ATTR_VALUE_LEN]
        assert result != "[REDACTED]"

    def test_short_safe_string_passes(self):
        assert redact("dashboard") == "dashboard"

    def test_empty_string_passes(self):
        assert redact("") == ""

    def test_password_pattern_redacted(self):
        assert redact("password=hunter2") == "[REDACTED]"

    def test_base64_encoded_credential_redacted(self):
        """Base64-encoded credential variants are decoded and redacted.

        Addresses the security-controls finding: redact() must catch
        base64-encoded credentials, not only raw patterns.
        """
        encoded_key = base64.b64encode(b"AKIAIOSFODNN7EXAMPLE1").decode("ascii")
        assert redact(encoded_key) == "[REDACTED]"

        encoded_pk = base64.b64encode(
            b"-----BEGIN RSA PRIVATE KEY-----"
        ).decode("ascii")
        assert redact(encoded_pk) == "[REDACTED]"

    def test_exfiltration_url_redacted(self):
        """Exfiltration URLs are scrubbed via canonical redact_exfiltration_urls.

        The fork's scrubber is content-based (query-param heuristics), not
        TLD-based, so use a long-query exfil shape it flags.
        """
        assert redact("https://attacker.example/c?x=" + "q" * 300) == "[REDACTED]"


# ---------------------------------------------------------------------------
# Reach of the entropy backstop — characterisation, not a target
# ---------------------------------------------------------------------------


class TestEntropyBackstopReach:
    """Pin where the entropy backstop can and cannot fire.

    These record CURRENT behaviour so the boundary is visible to the next
    reader, not a statement that the boundary is where it should be. Entropy
    over a value's own character frequencies is bounded by
    ``log2(distinct characters)``, so the 4.5 threshold needs >= 23 distinct
    characters before it is attainable at all. Anything that widens the
    backstop should update these, deliberately.

    Short hex staying visible is a separate, deliberate choice — see
    ``TestRedact.test_short_hex_32_chars_not_redacted`` and the 40-char pattern
    bound that spares trace ids.
    """

    def test_22_distinct_characters_cannot_reach_the_threshold(self):
        # log2(22) = 4.459 < 4.5: maximal entropy for this length, still under.
        value = "abcdefghijklmnopqrstuv"
        assert len(set(value)) == 22
        assert redact(value) == value

    def test_23_distinct_characters_is_the_first_reachable_length(self):
        # log2(23) = 4.524 > 4.5.
        value = "abcdefghijklmnopqrstuvw"
        assert len(set(value)) == 23
        assert redact(value) == "[REDACTED]"

    def test_a_small_alphabet_never_reaches_the_threshold(self):
        # 16 symbols cap entropy at log2(16) = 4.0 whatever the length, so the
        # backstop alone cannot fire on one. Deliberately not hex: hex of this
        # length is caught by the explicit pattern, which would hide the effect
        # being pinned here.
        value = ("qwertyuiopasdfgh" * 4)[:62]  # 62 chars, 16 distinct, non-hex
        assert len(set(value)) == 16
        assert redact(value) == value
