"""Tests for custom theme CSS injection sanitization.

Covers both backend (_sanitize_css_value, _validate_theme_data, _strip_to_allowed_vars)
and documents the security model:

  - Positive character allowlist: only [a-zA-Z0-9#(),.- %/] are permitted
  - Function denylist: url(), expression(), image(), image-set()
  - Key allowlist: only _THEME_CSS_VARS_SET keys accepted
  - Defense-in-depth: _strip_to_allowed_vars re-filters before disk write
"""

from __future__ import annotations

import pytest

from kiro_crew.dashboard.handlers import (
    _CSS_VALUE_ALLOWED_RE,
    _THEME_CSS_VARS_SET,
    _sanitize_css_value,
    _strip_to_allowed_vars,
    _validate_theme_data,
)


class TestSanitizeCssValue:
    """Unit tests for _sanitize_css_value positive allowlist."""

    # ── Safe values that MUST pass ──

    @pytest.mark.parametrize(
        "val",
        [
            "#12141a",
            "#fff",
            "#1a2b3c",
            "rgb(18, 20, 26)",
            "rgba(255, 255, 255, 0.1)",
            "hsl(220, 15%, 10%)",
            "hsla(220, 15%, 10%, 0.5)",
            "0 1px 3px rgba(0, 0, 0, 0.12)",  # shadow
            "0 4px 12px rgba(0, 0, 0, 0.25)",
            "none",
            "transparent",
            "1px",
            "50%",
            "0 0 0 2px rgba(99, 102, 241, 0.3)",  # ring
            "inset 0 1px 0 rgba(255, 255, 255, 0.05)",
            "rgb(255 255 255 / 0.1)",  # modern slash syntax
        ],
    )
    def test_safe_values_pass(self, val: str) -> None:
        assert _sanitize_css_value(val) is not None, f"safe value rejected: {val!r}"

    # ── Dangerous values that MUST be rejected ──

    @pytest.mark.parametrize(
        "val,reason",
        [
            # Semicolon injection — the #1 vector in the original code
            ("#fff; } body { background: red", "semicolon injection"),
            ("#fff;--evil:red", "mid-value semicolon"),
            ("red; }", "brace escape via semicolon"),
            # HTML escape from <style> tag
            ("</style><script>alert(1)</script>", "HTML tag injection"),
            ("<img src=x>", "HTML tag"),
            # CSS function injection
            ("url(//evil.com/track.gif)", "url() data exfil"),
            ("url('javascript:alert(1)')", "javascript: url"),
            ("expression(alert(1))", "IE expression()"),
            ("image(//evil.com/x.png)", "image() function"),
            ("image-set(//evil.com/x.png 1x)", "image-set() function"),
            # Backslash Unicode escapes
            ("\\0075rl(//evil.com)", "unicode escape for url"),
            ("\\00075rl(//evil.com)", "unicode escape variant"),
            # @-rule injection
            ("@import url(//evil.com)", "@import"),
            ("@charset 'utf-8'", "@charset"),
            # Quote injection
            ('"escape the attribute"', "double quote"),
            ("'escape the attribute'", "single quote"),
            # Brace injection
            ("} body { color: red", "closing brace"),
            ("{malicious}", "opening brace"),
            # Colon injection (could start new declarations in some contexts)
            ("red: value", "colon in value"),
            # Zero-width / control characters
            ("red\x00blue", "null byte"),
            # Excessive length
            ("a" * 201, "over 200 chars"),
            # Non-string types
            (123, "integer"),  # type: ignore[arg-type]
            (None, "None"),  # type: ignore[arg-type]
            (["red"], "list"),  # type: ignore[arg-type]
            # Empty / whitespace
            ("", "empty string"),
            ("   ", "whitespace only"),
            # IE-specific
            ("-moz-binding: url(evil.xml#xss)", "-moz-binding"),
            ("behavior: url(evil.htc)", "behavior property"),
        ],
    )
    def test_dangerous_values_rejected(self, val: object, reason: str) -> None:
        assert _sanitize_css_value(val) is None, f"dangerous value accepted ({reason}): {val!r}"  # type: ignore[arg-type]

    def test_trims_whitespace(self) -> None:
        result = _sanitize_css_value("  #fff  ")
        assert result == "#fff"


class TestValidateThemeData:
    """Tests for _validate_theme_data key and value validation."""

    def _minimal_theme(self, **overrides: object) -> dict:
        base: dict = {
            "name": "Test",
            "dark": {"--bg": "#000", "--text": "#fff", "--accent": "#00f"},
            "light": {"--bg": "#fff", "--text": "#000", "--accent": "#00f"},
        }
        base.update(overrides)
        return base

    def test_valid_theme_passes(self) -> None:
        assert _validate_theme_data(self._minimal_theme()) is None

    def test_unknown_key_rejected(self) -> None:
        theme = self._minimal_theme()
        theme["dark"]["--evil-custom"] = "#f00"
        err = _validate_theme_data(theme)
        assert err is not None
        assert "recognized" in err

    def test_key_not_starting_with_dashes_rejected(self) -> None:
        theme = self._minimal_theme()
        theme["dark"]["background"] = "#f00"
        err = _validate_theme_data(theme)
        assert err is not None

    def test_semicolon_injection_in_value_rejected(self) -> None:
        theme = self._minimal_theme()
        theme["dark"]["--bg"] = "#000; } * { background: red"
        err = _validate_theme_data(theme)
        assert err is not None
        assert "invalid" in err

    def test_url_injection_rejected(self) -> None:
        theme = self._minimal_theme()
        theme["dark"]["--bg"] = "url(//evil.com)"
        err = _validate_theme_data(theme)
        assert err is not None

    def test_missing_required_var(self) -> None:
        theme = self._minimal_theme()
        del theme["dark"]["--bg"]
        err = _validate_theme_data(theme)
        assert err is not None
        assert "missing" in err

    def test_name_required(self) -> None:
        theme = self._minimal_theme(name="")
        err = _validate_theme_data(theme)
        assert err is not None
        assert "name" in err

    def test_name_too_long(self) -> None:
        theme = self._minimal_theme(name="x" * 61)
        err = _validate_theme_data(theme)
        assert err is not None
        assert "too long" in err


class TestStripToAllowedVars:
    """Tests for _strip_to_allowed_vars defense-in-depth filter."""

    def test_keeps_known_vars(self) -> None:
        data = {"--bg": "#000", "--text": "#fff"}
        result = _strip_to_allowed_vars(data)
        assert result == {"--bg": "#000", "--text": "#fff"}

    def test_drops_unknown_vars(self) -> None:
        data = {"--bg": "#000", "--evil-custom": "#f00", "background": "red"}
        result = _strip_to_allowed_vars(data)
        assert "--evil-custom" not in result
        assert "background" not in result
        assert result == {"--bg": "#000"}

    def test_drops_unsafe_values(self) -> None:
        data = {"--bg": "#000", "--text": "url(//evil.com)"}
        result = _strip_to_allowed_vars(data)
        assert "--text" not in result
        assert result == {"--bg": "#000"}

    def test_trims_values(self) -> None:
        data = {"--bg": "  #000  "}
        result = _strip_to_allowed_vars(data)
        assert result["--bg"] == "#000"


class TestAllowlistCompleteness:
    """Verify the allowlist regex covers all needed CSS value patterns."""

    def test_regex_allows_hex_colors(self) -> None:
        for v in ("#fff", "#12ab3c", "#1a2B3C"):
            assert _CSS_VALUE_ALLOWED_RE.match(v), v

    def test_regex_allows_rgb_functions(self) -> None:
        assert _CSS_VALUE_ALLOWED_RE.match("rgb(0, 0, 0)")
        assert _CSS_VALUE_ALLOWED_RE.match("rgba(255, 255, 255, 0.5)")

    def test_regex_allows_hsl(self) -> None:
        assert _CSS_VALUE_ALLOWED_RE.match("hsl(220, 15%, 10%)")

    def test_regex_allows_shadow_values(self) -> None:
        assert _CSS_VALUE_ALLOWED_RE.match("0 1px 3px rgba(0, 0, 0, 0.12)")

    def test_regex_allows_modern_slash_syntax(self) -> None:
        assert _CSS_VALUE_ALLOWED_RE.match("rgb(255 255 255 / 0.1)")

    def test_regex_blocks_semicolons(self) -> None:
        assert not _CSS_VALUE_ALLOWED_RE.match("#fff; --x: red")

    def test_regex_blocks_braces(self) -> None:
        assert not _CSS_VALUE_ALLOWED_RE.match("} body {")

    def test_regex_blocks_backslash(self) -> None:
        assert not _CSS_VALUE_ALLOWED_RE.match("\\0075rl()")

    def test_regex_blocks_angle_brackets(self) -> None:
        assert not _CSS_VALUE_ALLOWED_RE.match("</style>")
        assert not _CSS_VALUE_ALLOWED_RE.match("<script>")

    def test_regex_blocks_at_sign(self) -> None:
        assert not _CSS_VALUE_ALLOWED_RE.match("@import")

    def test_regex_blocks_quotes(self) -> None:
        assert not _CSS_VALUE_ALLOWED_RE.match("'hello'")
        assert not _CSS_VALUE_ALLOWED_RE.match('"hello"')

    def test_regex_blocks_colon(self) -> None:
        assert not _CSS_VALUE_ALLOWED_RE.match("javascript:alert(1)")


class TestCssVarsSetSync:
    """Verify backend _THEME_CSS_VARS_SET matches the expected set of vars."""

    def test_required_vars_in_allowed_set(self) -> None:
        for v in ("--bg", "--text", "--accent"):
            assert v in _THEME_CSS_VARS_SET

    def test_shadow_vars_in_allowed_set(self) -> None:
        for v in ("--shadow-sm", "--shadow-md", "--shadow-lg"):
            assert v in _THEME_CSS_VARS_SET

    def test_random_unknown_not_in_set(self) -> None:
        assert "--evil" not in _THEME_CSS_VARS_SET
        assert "--background" not in _THEME_CSS_VARS_SET
        assert "background" not in _THEME_CSS_VARS_SET
