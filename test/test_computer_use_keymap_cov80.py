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
    KEY_ALIASES,
    KEY_NAMES,
    KEYCODES,
    MODIFIER_ALIASES,
    MODIFIER_NAMES,
    MODIFIERS,
    canonical_key,
    canonical_modifier,
    char_keystroke,
    parse_key,
    parse_spec,
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


class TestParseSpecIsTheCrossPlatformSeam:
    """``parse_spec`` is what the dispatch chokepoint validates through.

    ``tools._perform`` calls it instead of ``parse_key`` because
    ``parse_key``'s ``(keycode, flags)`` pair is macOS-shaped — Carbon keycodes
    and CoreGraphics flag masks — so validating through it would make the
    "refuse an unknown key BEFORE a driver synthesizes it" guarantee silently
    macOS-only. These tests pin the two properties that guarantee buys: the two
    parsers agree on what is legal, and the abstract result carries everything a
    platform table needs.
    """

    # Every accepted spelling plus every refusal shape the parser distinguishes.
    # Parametrized rather than looped so a divergence names the offending spec.
    @pytest.mark.parametrize(
        "spec",
        [
            "a",
            "A",
            "0",
            "cmd+s",
            "cmd+shift+a",
            "ctrl+c",
            "alt+tab",
            "super+l",
            "meta+v",
            "win+d",
            "fn+f1",
            "capslock+a",
            "shift+4",
            "$",
            "~",
            "?",
            "+",
            "shift++",
            "cmd++",
            "space",
            "return",
            "enter",
            "tab",
            "escape",
            "esc",
            "delete",
            "forwarddelete",
            "del",
            "pgup",
            "pagedown",
            "home",
            "arrowleft",
            "f1",
            "f20",
            "keypad0",
            "keypadenter",
            "mute",
            "minus",
            "equals",
            "backtick",
            "dot",
            "CMD+S",
            "",
            "   ",
            "\t",
            "cmd+",
            "bogus",
            "cmd+bogus",
            "hyper+a",
            "f21",
            "keypad10",
            "cmd shift a",
            "cmd-s",
        ],
    )
    def test_it_accepts_and_refuses_exactly_what_parse_key_does(self, spec: str) -> None:
        """A spec legal on one path must be legal on the other.

        A divergence would mean the chokepoint refuses a key the macOS driver
        would happily send, or admits one it cannot.
        """

        def refused(fn) -> bool:
            try:
                fn(spec)
                return False
            except KeyParseError:
                return True

        assert refused(parse_key) == refused(parse_spec)

    @pytest.mark.parametrize(
        ("alias", "canonical"),
        [
            ("esc", "escape"),
            ("pgup", "pageup"),
            ("pgdn", "pagedown"),
            ("pgdown", "pagedown"),
            ("enter", "return"),
            ("del", "forwarddelete"),
            ("dot", "period"),
            ("backtick", "grave"),
            ("equals", "equal"),
            ("arrowleft", "left"),
        ],
    )
    def test_aliases_collapse_to_one_canonical_key(self, alias: str, canonical: str) -> None:
        """So a platform table is keyed on physical keys, not on spellings.

        Without this, every table would have to repeat all 132 names and a
        forgotten alias would silently refuse a key the model spelled correctly.
        """
        assert parse_spec(alias).key == parse_spec(canonical).key

    def test_every_canonical_name_is_itself_a_known_key(self) -> None:
        """No alias may point at a name a table cannot look up."""
        assert not {name for name in KEY_ALIASES.values() if name not in KEY_NAMES}

    def test_canonicalization_preserves_the_keycode_and_is_idempotent(self) -> None:
        """The collapse must never move a spelling onto a DIFFERENT physical key.

        Asserted over the whole table: this is the property that lets a platform
        table trust ``canonical_key`` instead of re-deriving the aliases.

        Note this holds the MACOS keycode fixed, which is necessary but NOT
        sufficient — see the next test for the keys macOS conflates.
        """
        for name, keycode in KEYCODES.items():
            resolved = canonical_key(name)
            assert KEYCODES[resolved] == keycode
            assert canonical_key(resolved) == resolved

    @pytest.mark.parametrize(
        ("left", "right"),
        [("delete", "backspace"), ("help", "insert"), ("space", "spacebar")],
    )
    def test_keys_macos_conflates_stay_distinct(self, left: str, right: str) -> None:
        """macOS sharing a keycode must not merge two DIFFERENT physical keys.

        ``KEYCODES`` is a macOS table and macOS resolves several distinct keys onto
        one ``kVK_*`` code: ``delete``/``backspace`` are both 51, ``help``/``insert``
        both 114. Canonicalizing purely by keycode identity therefore made
        ``canonical_key("delete") == "backspace"`` — right for macOS, wrong
        everywhere else, where ``VK_DELETE`` and ``VK_BACK`` are separate keys. A
        Windows table keyed on the merged name would send Backspace for
        ``press_key("delete")``, deleting the character BEFORE the caret instead of
        after it, and leave ``VK_DELETE``/``VK_INSERT`` unreachable.
        """
        assert canonical_key(left) == left
        assert canonical_key(right) == right
        assert parse_spec(left).key != parse_spec(right).key

    def test_an_unknown_name_passes_through_rather_than_raising(self) -> None:
        """``canonical_key`` normalizes; ``parse_spec`` is the layer that refuses."""
        assert canonical_key("definitely-not-a-key") == "definitely-not-a-key"

    @pytest.mark.parametrize("spec", ["$", "A", "?", "+"])
    def test_a_shift_demanding_glyph_carries_shift_in_modifiers(self, spec: str) -> None:
        """A table must not have to re-derive that ``$`` is Shift+4.

        Reported in ``modifiers`` rather than a separate flag, so a table reads one
        place. A driver that consulted only ``modifiers`` while the requirement lived
        on a second field would send the UNSHIFTED key — ``4`` for ``$``.
        """
        assert "shift" in parse_spec(spec).modifiers

    @pytest.mark.parametrize("spec", ["a", "4", "escape"])
    def test_an_unshifted_key_carries_no_shift(self, spec: str) -> None:
        assert "shift" not in parse_spec(spec).modifiers

    def test_an_implied_shift_equals_a_written_one(self) -> None:
        """The equality ``KeySpec`` promises: same keystroke -> same spec.

        A platform table keyed on ``modifiers`` therefore cannot treat ``A`` and
        ``shift+a`` as different events.
        """
        assert parse_spec("A") == parse_spec("shift+a")
        assert parse_spec("$") == parse_spec("shift+4")

    def test_modifiers_are_names_not_masks(self) -> None:
        """The abstract layer must carry no platform numbers at all.

        A mask here would be a CoreGraphics value, which is exactly the coupling
        this seam exists to remove.
        """
        spec = parse_spec("cmd+shift+a")
        assert spec.modifiers == frozenset({"ctrl", "shift"})
        assert spec.key == "a"

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            ("ctrl+c", "control+c"),
            ("alt+tab", "option+tab"),
            ("opt+tab", "alt+tab"),
            ("cmd+s", "ctrl+s"),
            ("super+s", "meta+s"),
            ("fn+f1", "function+f1"),
        ],
    )
    def test_modifier_aliases_collapse_to_one_canonical_name(self, left: str, right: str) -> None:
        """The seam must normalize MODIFIERS too, not just keys.

        A Windows VK table doing the natural ``"ctrl" in spec.modifiers`` would
        otherwise drop the modifier from ``control+c`` and send a bare ``c`` into a
        live window — the "a different keystroke than the caller asked for" failure
        ``parse_spec`` exists to prevent, and worse than a refusal because it
        succeeds.
        """
        assert parse_spec(left) == parse_spec(right)

    def test_every_canonical_modifier_is_itself_a_known_modifier(self) -> None:
        """No alias may point at a name a table cannot look up."""
        assert not {name for name in MODIFIER_ALIASES.values() if name not in MODIFIER_NAMES}

    @pytest.mark.parametrize("spelling", ["cmd", "command", "super", "meta"])
    def test_the_command_spellings_mean_CTRL_not_the_logo_key(self, spelling: str) -> None:
        """**The data-loss defect this test exists for.**

        What a caller means by ``cmd+s`` is Save, and on Windows that is Ctrl+S.
        Canonicalizing these onto the logo key sent Win+S — which opens Search, leaves
        the document unsaved, and loses the edits on the close that follows. The
        mapping follows the INTENT, not the key's name.
        """
        assert canonical_modifier(spelling) == "ctrl"
        assert parse_spec(f"{spelling}+s") == parse_spec("ctrl+s")

    def test_win_still_names_the_PHYSICAL_logo_key(self) -> None:
        """The one spelling that is about the key rather than the intent.

        Folding it onto ctrl too would make ``win+d`` (Show Desktop) unreachable, so
        the two must stay distinct.
        """
        assert canonical_modifier("win") == "win"
        assert parse_spec("win+d") != parse_spec("ctrl+d")

    def test_macos_is_unaffected_by_the_windows_intent_mapping(self) -> None:
        """``parse_key`` reads ``MODIFIERS``, not ``MODIFIER_ALIASES``.

        On macOS every one of these spellings is ``FLAG_COMMAND``, which is correct
        there — so the Windows-facing canonicalization must not leak into that path.
        """
        for spelling in ("cmd", "command", "super", "meta", "win"):
            assert parse_key(f"{spelling}+s")[1] == FLAG_COMMAND

    def test_the_vocabulary_sets_match_the_tables(self) -> None:
        assert KEY_NAMES == frozenset(KEYCODES)
        assert MODIFIER_NAMES == frozenset(MODIFIERS)


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
