"""Tests for ``_safe_color``, the hex-colour validator the avatar fields use.

Covers valid hex, malformed and non-string input.
"""

from kiro_crew.config.loader import _safe_color


class TestSafeColor:
    """Unit tests for _safe_color validator."""

    def test_valid_lowercase(self):
        assert _safe_color("#aabbcc") == "#aabbcc"

    def test_valid_uppercase_normalized(self):
        assert _safe_color("#AABBCC") == "#aabbcc"

    def test_valid_mixed_case_normalized(self):
        assert _safe_color("#4A90D9") == "#4a90d9"

    def test_empty_string(self):
        assert _safe_color("") == ""

    def test_none(self):
        assert _safe_color(None) == ""

    def test_non_string_int(self):
        assert _safe_color(123) == ""

    def test_non_string_list(self):
        assert _safe_color(["#aabbcc"]) == ""

    def test_non_string_bool(self):
        assert _safe_color(True) == ""

    def test_too_short(self):
        """3-digit shorthand is NOT accepted (must be 6-digit)."""
        assert _safe_color("#abc") == ""

    def test_too_long(self):
        assert _safe_color("#aabbccdd") == ""

    def test_no_hash(self):
        assert _safe_color("aabbcc") == ""

    def test_invalid_chars(self):
        assert _safe_color("#gggggg") == ""

    def test_whitespace_trimmed(self):
        assert _safe_color("  #aabbcc  ") == "#aabbcc"
