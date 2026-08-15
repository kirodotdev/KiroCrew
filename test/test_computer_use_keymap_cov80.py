"""``computer_use.keymap`` — the refusal and implied-shift branches of the parser.

Every uncovered line in this module is a place where a wrong answer would post a
DIFFERENT keystroke than the caller asked for into a live application, which is
exactly why the module refuses instead of guessing. Pinned here:

* the four ``KeyParseError`` exits (empty spec, non-string, unknown modifier,
  unknown key);
* the bare-``+`` and trailing-``+`` spellings, which have to be special-cased
  before the split or the key part comes back empty;
* the implied-shift resolutions (``$`` -> Shift+4, ``A`` -> Shift+a) and their
  agreement with the explicit ``shift+`` spelling;
* ``char_keystroke``'s two refusals (empty string, unreachable character).
"""

from __future__ import annotations

import pytest

from kiro_crew.computer_use.keymap import (
    FLAG_ALTERNATE,
    FLAG_COMMAND,
    FLAG_SHIFT,
    KEYCODES,
    char_keystroke,
    parse_key,
)
from kiro_crew.computer_use.types import KeyParseError


class TestParseKeyRefuses:
    @pytest.mark.parametrize("spec", ["", "   ", "\t"])
    def test_empty_spec(self, spec: str) -> None:
        with pytest.raises(KeyParseError, match="empty key spec"):
            parse_key(spec)

    def test_non_string_spec(self) -> None:
        with pytest.raises(KeyParseError, match="empty key spec"):
            parse_key(None)  # type: ignore[arg-type]

    def test_unknown_modifier_is_refused_rather_than_dropped(self) -> None:
        with pytest.raises(KeyParseError, match="unknown modifier 'hyper'"):
            parse_key("hyper+a")

    def test_unknown_key_is_refused(self) -> None:
        with pytest.raises(KeyParseError, match="unknown key 'nope'"):
            parse_key("cmd+nope")

    def test_a_spec_of_only_separators_still_names_the_plus_key(self) -> None:
        """``"+ +"`` strips to a trailing-plus spec: the whitespace token drops out
        and the plus survives, so this is the plus key rather than a refusal."""
        from kiro_crew.computer_use.keymap import KEYCODES as _kc

        assert parse_key("+ +") == (_kc["="], FLAG_SHIFT)


class TestParseKeyPlusSpellings:
    def test_a_bare_plus_is_the_plus_key(self) -> None:
        keycode, flags = parse_key("+")
        assert keycode == KEYCODES["="]
        assert flags == FLAG_SHIFT

    def test_a_trailing_plus_keeps_its_modifiers(self) -> None:
        keycode, flags = parse_key("cmd++")
        assert keycode == KEYCODES["="]
        assert flags == FLAG_COMMAND | FLAG_SHIFT

    def test_whitespace_around_tokens_is_tolerated(self) -> None:
        assert parse_key("  option + tab ") == (KEYCODES["tab"], FLAG_ALTERNATE)


class TestImpliedShift:
    def test_a_shifted_glyph_implies_shift(self) -> None:
        assert parse_key("$") == (KEYCODES["4"], FLAG_SHIFT)

    def test_an_uppercase_letter_implies_shift(self) -> None:
        assert parse_key("A") == (KEYCODES["a"], FLAG_SHIFT)

    def test_explicit_and_implied_shift_produce_the_same_event(self) -> None:
        assert parse_key("shift+a") == parse_key("A")

    def test_a_modifier_ors_into_an_implied_shift(self) -> None:
        keycode, flags = parse_key("cmd+A")
        assert keycode == KEYCODES["a"]
        assert flags == FLAG_COMMAND | FLAG_SHIFT


class TestCharKeystroke:
    def test_empty_string_has_no_keystroke(self) -> None:
        assert char_keystroke("") is None

    def test_unreachable_character_returns_none_rather_than_a_substitute(self) -> None:
        assert char_keystroke("é") is None
        assert char_keystroke("字") is None

    def test_plain_character(self) -> None:
        assert char_keystroke("k") == (KEYCODES["k"], 0)

    def test_shifted_character_carries_the_shift_flag(self) -> None:
        assert char_keystroke("%") == (KEYCODES["5"], FLAG_SHIFT)

    def test_uppercase_character_carries_the_shift_flag(self) -> None:
        assert char_keystroke("Z") == (KEYCODES["z"], FLAG_SHIFT)
