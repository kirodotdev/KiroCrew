"""Tests for the shared slug helpers.

The four backend slug builders fall back to ``slug_hash_fallback`` when a
name filters to the empty string, so distinct non-ASCII names must derive
distinct, stable, pattern-safe identifiers.

The slug validators that refuse rather than derive (papyrus project names,
code-review-sage namespaces) report the refused character through
``refused_slug_char``, so the dashboard can name it in the user's language.
"""

from __future__ import annotations

import re

import pytest

from kiro_crew.slugs import SLUG_CHARS, refused_slug_char, slug_hash_fallback

_SHAPE_RE = re.compile(r"^[a-z0-9-]+-[0-9a-f]{16}$")


class TestSlugHashFallback:
    def test_distinct_inputs_derive_distinct_ids(self) -> None:
        assert slug_hash_fallback("\u4f1a\u8bae\u7eaa\u8981", "artifact") != slug_hash_fallback(
            "\u8cb7\u3044\u7269\u30ea\u30b9\u30c8", "artifact"
        )

    def test_same_input_is_stable(self) -> None:
        assert slug_hash_fallback("\u4f1a\u8bae\u7eaa\u8981", "workflow") == slug_hash_fallback(
            "\u4f1a\u8bae\u7eaa\u8981", "workflow"
        )

    def test_shape_is_prefix_dash_16_hex(self) -> None:
        out = slug_hash_fallback("\u0440\u0430\u0431\u043e\u0442\u0430", "instance")
        assert out.startswith("instance-")
        assert _SHAPE_RE.match(out)

    def test_prefix_separates_namespaces(self) -> None:
        assert slug_hash_fallback("\u4f1a\u8bae", "artifact") != slug_hash_fallback(
            "\u4f1a\u8bae", "custom"
        )

    def test_lone_surrogate_input_is_total(self) -> None:
        # A JSON string escape can decode to a lone surrogate; the fallback
        # must derive an id for it rather than raise UnicodeEncodeError.
        out = slug_hash_fallback("\ud800", "artifact")
        assert _SHAPE_RE.match(out)
        assert out == slug_hash_fallback("\ud800", "artifact")
        assert out != slug_hash_fallback("\udfff", "artifact")


class TestRefusedSlugChar:
    @pytest.mark.parametrize(
        ("typed", "expected"),
        [
            ("\u8bba\u6587", "\u8bba"),  # Han: the first letter is the refusal
            ("\ubd84\uae30", "\ubd84"),  # Hangul
            ("\u062a\u0642\u0631\u064a\u0631", "\u062a"),  # Arabic
            ("my-\u8bba\u6587", "\u8bba"),  # the first refused character, not the first character
            ("a/b", "/"),
            ("a\\b", "\\"),
        ],
    )
    def test_names_the_first_character_the_slug_cannot_hold(
        self, typed: str, expected: str
    ) -> None:
        assert refused_slug_char(typed) == expected

    @pytest.mark.parametrize(
        ("typed", "expected"),
        [
            # The capital Cyrillic letter the user typed, never its lowercase form,
            # which renders like the Latin "o" the rule allows.
            ("\u041e\u0442\u0447\u0435\u0442", "\u041e"),
            # Dotted capital I lowercases to "i" + U+0307: the report is still the
            # letter typed, not the combining dot lowercasing produced.
            ("\u0130stanbul", "\u0130"),
            ("M\u00dcNCHEN", "\u00dc"),
            ("\u0391\u03b8\u03ae\u03bd\u03b1", "\u0391"),  # Greek capital Alpha
        ],
    )
    def test_reports_the_character_as_typed_before_lowercasing(
        self, typed: str, expected: str
    ) -> None:
        assert refused_slug_char(typed) == expected

    @pytest.mark.parametrize("typed", ["My Paper", "Team-A", "  Draft  2  ", "a\tb", "\u212a"])
    def test_what_the_normalizer_turns_into_slug_characters_is_not_blamed(self, typed: str) -> None:
        # Spaces become "-" and ASCII capitals become lowercase, so neither is the
        # refused character; the Kelvin sign lowercases to an ASCII "k".
        assert refused_slug_char(typed) == ""

    def test_a_decomposed_letter_is_reported_as_the_letter_typed(self) -> None:
        # NFD "Mu" + U+0308: the user typed "u-umlaut", not a bare combining mark.
        assert refused_slug_char("Mu\u0308nchen") == "\u00fc"
        assert refused_slug_char("M\u00fcnchen") == "\u00fc"

    @pytest.mark.parametrize(
        ("typed", "expected"),
        [
            ("a\u202eb", "U+202E"),  # bidi override: would reorder the message naming it
            ("a\u200bb", "U+200B"),  # zero-width space: would be invisible inside quotes
            ("x\u0301", "U+0301"),  # a combining mark NFC cannot compose onto its base
            ("a\x00b", "U+0000"),
            ("a\x07b", "U+0007"),
            ("a\ud800", "U+D800"),  # a lone surrogate is reportable, not a crash
        ],
    )
    def test_a_character_that_cannot_stand_alone_is_reported_as_its_code_point(
        self, typed: str, expected: str
    ) -> None:
        assert refused_slug_char(typed) == expected

    @pytest.mark.parametrize("typed", ["", "paper", ".hidden", "-rf", "x" * 200])
    def test_empty_when_every_character_is_a_slug_character(self, typed: str) -> None:
        # These fail on length or on a character's position, which the caller's own
        # rule explains; no single character is to blame.
        assert refused_slug_char(typed) == ""

    def test_the_slug_alphabet_is_lowercase_ascii_digits_and_three_marks(self) -> None:
        assert SLUG_CHARS == frozenset("abcdefghijklmnopqrstuvwxyz0123456789._-")
