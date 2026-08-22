"""Tests for the crew-variables lexical core.

Pure in-memory: no filesystem, no config, no child processes. The one test that
reads source files reads them only to enforce the reserved-token ratchet.
"""

from __future__ import annotations

import re
from pathlib import Path

import kiro_crew
from kiro_crew.variables import (
    MAX_VALUE_LEN,
    RESERVED_TOKENS,
    expand,
    validate_pair,
)


class TestExpandGrammar:
    def test_substitutes_a_known_name(self):
        out, unresolved = expand("hit {{baseUrl}}/health", {"baseUrl": "https://x.test"})
        assert out == "hit https://x.test/health"
        assert unresolved == frozenset()

    def test_tolerates_interior_whitespace(self):
        out, _ = expand("{{ baseUrl }}", {"baseUrl": "v"})
        assert out == "v"

    def test_unknown_name_is_left_byte_identical(self):
        out, unresolved = expand("hit {{nope}}/health", {"baseUrl": "v"})
        assert out == "hit {{nope}}/health"
        assert unresolved == frozenset({"nope"})

    def test_unknown_name_is_never_blanked(self):
        # Blanking would turn this into a different, still-plausible instruction.
        out, _ = expand("curl {{baseUrl}}/health", {"other": "v"})
        assert "curl {{baseUrl}}/health" == out

    def test_multiple_tokens_and_repeats(self):
        out, unresolved = expand("{{a}}-{{b}}-{{a}}", {"a": "1", "b": "2"})
        assert out == "1-2-1"
        assert unresolved == frozenset()

    def test_reports_every_distinct_unresolved_name(self):
        _, unresolved = expand("{{x}} {{y}} {{x}}", {"z": "1"})
        assert unresolved == frozenset({"x", "y"})


class TestExpandMalformedTokens:
    """A malformed token is not a variable reference, so it is neither
    substituted nor reported — reporting it would train users to ignore the
    unresolved-name warning."""

    def test_empty_braces(self):
        out, unresolved = expand("{{ }}", {"a": "1"})
        assert out == "{{ }}"
        assert unresolved == frozenset()

    def test_leading_digit(self):
        out, unresolved = expand("{{1abc}}", {"a": "1"})
        assert out == "{{1abc}}"
        assert unresolved == frozenset()

    def test_hyphen_in_name(self):
        out, unresolved = expand("{{a-b}}", {"a": "1"})
        assert out == "{{a-b}}"
        assert unresolved == frozenset()

    def test_single_braces_are_not_tokens(self):
        out, unresolved = expand("{a}", {"a": "1"})
        assert out == "{a}"
        assert unresolved == frozenset()


class TestExpandIsSinglePass:
    def test_a_substituted_value_is_not_rescanned(self):
        # 'other' is defined, so a second pass WOULD resolve it. It must not.
        out, unresolved = expand("{{a}}", {"a": "{{other}}", "other": "boom"})
        assert out == "{{other}}"
        assert unresolved == frozenset()

    def test_a_self_referential_value_terminates(self):
        out, _ = expand("{{a}}", {"a": "{{a}}"})
        assert out == "{{a}}"


class TestExpandReplacementSafety:
    """re.sub must not interpret the VALUE as a replacement template."""

    def test_numeric_backreference_is_literal(self):
        out, _ = expand("{{a}}", {"a": r"\1"})
        assert out == r"\1"

    def test_named_group_reference_is_literal(self):
        out, _ = expand("{{a}}", {"a": r"\g<0>"})
        assert out == r"\g<0>"

    def test_backslash_run_is_literal(self):
        out, _ = expand("{{a}}", {"a": r"C:\path\to"})
        assert out == r"C:\path\to"


class TestExpandEmptyMapping:
    def test_returns_the_identical_object_unscanned(self):
        text = "nothing to do {{a}}"
        out, unresolved = expand(text, {})
        assert out is text
        assert unresolved == frozenset()

    def test_empty_text_is_returned_as_is(self):
        out, unresolved = expand("", {"a": "1"})
        assert out == ""
        assert unresolved == frozenset()


class TestValidatePairAccepts:
    def test_plain_string(self):
        assert validate_pair("baseUrl", "https://x.test") == ("baseUrl", "https://x.test")

    def test_underscores_and_digits(self):
        assert validate_pair("a_1B", "v") == ("a_1B", "v")

    def test_empty_string_is_a_legitimate_value(self):
        # An empty value at a narrow scope is a deliberate override, not 'unset'.
        assert validate_pair("a", "") == ("a", "")

    def test_tab_is_allowed(self):
        assert validate_pair("a", "x\ty") == ("a", "x\ty")

    def test_bool_takes_its_json_spelling(self):
        assert validate_pair("a", True) == ("a", "true")
        assert validate_pair("a", False) == ("a", "false")

    def test_int_and_float_are_coerced(self):
        assert validate_pair("a", 3) == ("a", "3")
        assert validate_pair("a", 1.5) == ("a", "1.5")

    def test_value_at_the_length_cap(self):
        key, value = validate_pair("a", "x" * MAX_VALUE_LEN)
        assert key == "a"
        assert value is not None and len(value) == MAX_VALUE_LEN


class TestValidatePairRejects:
    def test_leading_digit(self):
        key, reason = validate_pair("1abc", "v")
        assert key is None
        assert "letter" in reason

    def test_hyphen(self):
        assert validate_pair("a-b", "v")[0] is None

    def test_empty_name(self):
        assert validate_pair("", "v")[0] is None

    def test_non_string_name(self):
        assert validate_pair(3, "v")[0] is None

    def test_reserved_name(self):
        key, reason = validate_pair("MAX_SUBAGENTS", "v")
        assert key is None
        assert "reserved" in reason

    def test_reserved_single_brace_name(self):
        assert validate_pair("bot_name", "v")[0] is None

    def test_uncoercible_type(self):
        key, reason = validate_pair("a", {"nested": 1})
        assert key is None
        assert "string" in reason

    def test_none_value(self):
        assert validate_pair("a", None)[0] is None

    def test_oversize_value(self):
        key, reason = validate_pair("a", "x" * (MAX_VALUE_LEN + 1))
        assert key is None
        assert str(MAX_VALUE_LEN) in reason

    def test_newline_is_a_control_character(self):
        # A multi-line value could forge a context header in the assembled prompt.
        key, reason = validate_pair("a", "one\ntwo")
        assert key is None
        assert "control character" in reason

    def test_carriage_return_and_nul(self):
        assert validate_pair("a", "x\ry")[0] is None
        assert validate_pair("a", "x\x00y")[0] is None

    def test_delete_character(self):
        assert validate_pair("a", "x\x7fy")[0] is None


class TestReservedTokenRatchet:
    """Every ``{{...}}`` literal the gateway itself resolves must be reserved.

    Without this, a new built-in token could be added while a user variable of
    the same name silently shadows nothing and looks broken.
    """

    def test_covers_every_builtin_token_in_source(self):
        root = Path(kiro_crew.__file__).resolve().parent
        sources = [
            root / "context.py",
            root / "dashboard" / "handlers" / "autonudge.py",
            root / "slack_manifest.py",
        ]
        pattern = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}")
        found: set[str] = set()
        for path in sources:
            assert path.exists(), f"expected source file missing: {path}"
            found |= set(pattern.findall(path.read_text(encoding="utf-8")))

        assert found, "no builtin tokens found — the ratchet would be vacuous"
        missing = found - RESERVED_TOKENS
        assert not missing, f"built-in prompt tokens not in RESERVED_TOKENS: {sorted(missing)}"


class TestExpansionIsIdempotent:
    """The single-pass rule has to survive a message crossing MORE THAN ONE
    expansion boundary.

    An auto-nudge body is expanded once with the loop's armed crew and then again
    by the transport's own inbound expansion. Each call is single-pass, but two in
    series would together resolve a token that arrived from a value — the indirect
    expansion single-pass exists to forbid. Refusing '{{' in a value is what makes
    the composition safe, so no boundary needs to know what the others did.
    """

    def test_a_value_carrying_a_token_is_refused(self):
        name, reason = validate_pair("outer", "prefix {{inner}} suffix")
        assert name is None
        assert "{{" in reason

    def test_a_value_carrying_a_bare_opening_delimiter_is_refused(self):
        # Refused too: two values could otherwise supply the halves of a token.
        name, _ = validate_pair("outer", "trailing {{")
        assert name is None

    def test_a_lone_closing_delimiter_stays_legal(self):
        # Harmless on its own, and refusing it would reject ordinary prose.
        name, value = validate_pair("outer", "closing }} only")
        assert name == "outer"
        assert value == "closing }} only"

    def test_expanding_twice_matches_expanding_once(self):
        """The property the refusal buys, stated directly."""
        values = {"a": "A", "b": "B"}
        for text in (
            "{{a}} and {{b}}",
            "no tokens here",
            "{{a}} {{missing}} {{b}}",
            "{{a}}{{a}}{{b}}",
        ):
            once, unresolved_once = expand(text, values)
            twice, unresolved_twice = expand(once, values)
            assert twice == once, f"second pass changed {text!r}"
            assert unresolved_twice == unresolved_once

    def test_a_value_that_looks_like_a_token_cannot_be_smuggled_in(self):
        """The refusal is the ONLY thing standing between two boundaries and
        indirect expansion, so this pins both halves of that claim."""
        # Half one: such a value can never be stored.
        assert validate_pair("indirect", "{{a}}")[0] is None

        # Half two: why that matters. If it COULD be stored, two single-pass
        # boundaries in series would resolve it — each call is well-behaved and
        # the composition is still unsafe. This is the vulnerability the refusal
        # removes at the source, not a behaviour to rely on.
        once, _ = expand("{{indirect}}", {"indirect": "{{a}}"})
        assert once == "{{a}}", "one pass must not resolve through a value"
        twice, _ = expand(once, {"indirect": "{{a}}", "a": "A"})
        assert twice == "A", (
            "two boundaries resolve a token that came from a value — which is "
            "why validate_pair refuses '{{' rather than each boundary trying to "
            "detect whether another one already ran"
        )
