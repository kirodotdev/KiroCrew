"""Tests for configurable bot_name — substitution, defaults, sanitization."""

from __future__ import annotations

import unicodedata

import pytest

from kiro_crew.config import loader
from kiro_crew.config.loader import _sanitize_bot_name

# Non-ASCII fixtures are spelled as escapes so the file stays ASCII; the
# comment beside each names what the reader would see.
_JOSE_NFC = "Jos\u00e9"  # Jose with a precomposed e-acute
_JOSE_NFD = "Jose\u0301"  # the same name, e + COMBINING ACUTE ACCENT
_KOREAN = "\uae30\ub85c"  # Hangul syllables
_JAPANESE = "\u30ad\u30ed\u5c0f\u5ddd"  # katakana + kanji
_CHINESE = "\u5c0f\u7231"  # CJK ideographs
_CYRILLIC = "\u041a\u0438\u0440\u043e"  # Cyrillic capital Ka, i, er, o
_HINDI = "\u0939\u0928\u094d\u0926\u0940"  # Devanagari letters with a virama and vowel sign
_THAI_WATER = "\u0e19\u0e49\u0e33"  # Thai letters with a tone mark and SARA AM
_ARABIC_NAME = "\u0645\u064f\u062d\u064e\u0645\u0651\u062f"  # Arabic letters with harakat
_FULLWIDTH_KIRO = "\uff4b\uff49\uff52\uff4f"  # FULLWIDTH LATIN SMALL LETTERS k i r o
_SPARKLES = "\u2728"  # an emoji (category So)
_VS16 = "\ufe0f"  # VARIATION SELECTOR-16: a mark (Mn) that renders as nothing
_KEYCAP = "\u20e3"  # COMBINING ENCLOSING KEYCAP (Me)


class TestSanitizeBotName:
    def test_normal_name(self):
        assert _sanitize_bot_name("Alita") == "Alita"

    def test_empty_returns_empty(self):
        assert _sanitize_bot_name("") == ""

    def test_strips_braces(self):
        assert _sanitize_bot_name("{bot_name}") == "bot_name"

    def test_strips_markdown(self):
        assert _sanitize_bot_name("**Bold**") == "Bold"

    def test_max_length(self):
        assert len(_sanitize_bot_name("A" * 100)) == 50

    def test_non_string(self):
        assert _sanitize_bot_name(123) == ""  # type: ignore[arg-type]

    def test_whitespace_stripped(self):
        assert _sanitize_bot_name("  Kiro  ") == "Kiro"

    def test_ascii_separators_kept(self):
        assert _sanitize_bot_name("Kiro_Bot 2.0-beta") == "Kiro_Bot 2.0-beta"

    def test_ascii_punctuation_and_symbols_dropped(self):
        assert _sanitize_bot_name("Kiro! #1 @home") == "Kiro 1 home"


class TestSanitizeBotNameUnicode:
    """A name is the user's alphabet, not ASCII: the character policy of the artifact tag rule."""

    def test_accented_latin_name_keeps_its_accents(self):
        assert _sanitize_bot_name("Jos\u00e9 Mu\u00f1oz") == "Jos\u00e9 Mu\u00f1oz"
        assert _sanitize_bot_name("Ren\u00e9e") == "Ren\u00e9e"

    def test_nfc_and_nfd_spellings_sanitize_to_one_string(self):
        assert _JOSE_NFC != _JOSE_NFD  # the fixture is two spellings of one name
        assert _sanitize_bot_name(_JOSE_NFD) == _sanitize_bot_name(_JOSE_NFC) == _JOSE_NFC

    @pytest.mark.parametrize(
        "between",
        ["\u034f", "\u200b", "\u2728"],
        ids=["grapheme-joiner", "zero-width-space", "emoji"],
    )
    def test_output_is_nfc_when_a_removal_exposes_a_composable_pair(self, between):
        # "e" + <dropped character> + COMBINING ACUTE ACCENT: the input is NFC
        # (the pair is not adjacent), the removal makes it adjacent, and the
        # stored spelling must still be the precomposed e-acute.
        out = _sanitize_bot_name("Jose" + between + "\u0301")
        assert out == _JOSE_NFC
        assert unicodedata.is_normalized("NFC", out)

    @pytest.mark.parametrize(
        "name",
        [_KOREAN, _JAPANESE, _CHINESE, _CYRILLIC, _HINDI, _THAI_WATER, _ARABIC_NAME],
        ids=["korean", "japanese", "chinese", "cyrillic", "hindi", "thai", "arabic"],
    )
    def test_names_in_other_scripts_survive(self, name):
        assert _sanitize_bot_name(name) == name

    def test_non_ascii_decimal_digits_survive(self):
        # ARABIC-INDIC DIGIT THREE is a decimal digit (Nd), like "3".
        assert _sanitize_bot_name("Kiro \u0663") == "Kiro \u0663"

    def test_letter_numbers_survive_as_the_tag_rule_admits_them(self):
        # IDEOGRAPHIC NUMBER ZERO (Nl) is written in CJK names and in the
        # Japanese placeholder name "maru-maru"; the tag rule admits Nl.
        assert _sanitize_bot_name("\u5c0f\u3007") == "\u5c0f\u3007"
        assert (
            _sanitize_bot_name("\u3007\u3007\u3061\u3083\u3093") == "\u3007\u3007\u3061\u3083\u3093"
        )

    def test_other_numbers_are_dropped(self):
        # SUPERSCRIPT TWO (No) is a symbol drawn from a digit, not a digit.
        assert _sanitize_bot_name("Kiro\u00b2") == "Kiro"

    def test_stacked_marks_are_kept(self):
        # Hebrew pointing (dagesh, a vowel, a cantillation mark) and a Tibetan
        # stack are writing: every mark stays on its base.
        hebrew = unicodedata.normalize(
            "NFC", "\u05e9\u05bc\u05b8\u0591\u05dc"  # shin + dagesh + qamats + etnahta, lamed
        )
        assert _sanitize_bot_name(hebrew) == hebrew
        tibetan = "\u0f66\u0fa4\u0fb1\u0f72"  # sa + subjoined pa + subjoined ya + vowel i
        assert _sanitize_bot_name(tibetan) == tibetan

    def test_zero_width_characters_are_still_removed(self):
        assert _sanitize_bot_name("Ki\u200bro") == "Kiro"  # ZERO WIDTH SPACE
        assert _sanitize_bot_name("Ki\u200dro") == "Kiro"  # ZERO WIDTH JOINER
        assert _sanitize_bot_name("Ki\u2060ro") == "Kiro"  # WORD JOINER
        assert _sanitize_bot_name("\ufeffKiro") == "Kiro"  # ZERO WIDTH NO-BREAK SPACE

    def test_bidi_controls_are_still_removed(self):
        # RIGHT-TO-LEFT OVERRIDE ... POP DIRECTIONAL FORMATTING
        assert _sanitize_bot_name("\u202eKiro\u202c") == "Kiro"
        # LEFT-TO-RIGHT ISOLATE ... POP DIRECTIONAL ISOLATE
        assert _sanitize_bot_name("\u2066Kiro\u2069") == "Kiro"

    def test_bidi_controls_inside_a_cyrillic_name_are_removed(self):
        assert _sanitize_bot_name("\u202e" + _CYRILLIC + "\u202c") == _CYRILLIC

    def test_invisible_letters_and_marks_are_removed(self):
        # A variation selector is a mark by category but renders as nothing: the
        # residue an emoji such as a red heart leaves once the emoji is dropped.
        assert _sanitize_bot_name("Kiro" + _VS16) == "Kiro"
        assert _sanitize_bot_name("Kiro \u2764" + _VS16) == "Kiro"
        # The Hangul fillers are letters by category and also render as nothing.
        assert _sanitize_bot_name(_KOREAN + "\u3164") == _KOREAN
        assert _sanitize_bot_name("\u115f" + _KOREAN) == _KOREAN

    def test_every_default_ignorable_code_point_is_removed(self):
        """The tag rule's published invisible table is removed here too.

        The filter reads ``terminal_safe._is_invisible`` for the letters and
        marks that render as nothing and relies on the category test for the
        format and unassigned ones; this test checks the two together against
        the artifact store's full published table, code point by code point.
        """
        from kiro_crew.artifact_store.rules import _DEFAULT_IGNORABLE

        kept = sorted(
            cp for cp in _DEFAULT_IGNORABLE if _sanitize_bot_name(f"K{chr(cp)}iro") != "Kiro"
        )
        assert kept == [], [f"U+{cp:04X} {unicodedata.category(chr(cp))}" for cp in kept[:10]]

    def test_braces_are_still_removed_around_a_unicode_name(self):
        assert _sanitize_bot_name("{" + _KOREAN + "}") == _KOREAN
        assert _sanitize_bot_name("{bot_name}") == "bot_name"

    def test_a_space_left_dangling_by_a_removed_brace_is_folded(self):
        # A brace is a removed character like any other, so the two spaces it
        # kept apart fold to one.
        assert _sanitize_bot_name("Kiro { Bot") == "Kiro Bot"
        assert _sanitize_bot_name("Kiro { } Bot") == "Kiro Bot"

    def test_length_cap_holds_and_counts_nfc_code_points(self):
        assert len(_sanitize_bot_name(_KOREAN * 50)) == 50
        # 60 decomposed e-acutes are 120 code points typed and 60 letters read;
        # the cap keeps 50 letters, not 25 bare "e"s.
        assert _sanitize_bot_name("e\u0301" * 60) == "\u00e9" * 50

    def test_emoji_are_dropped_as_the_tag_rule_refuses_them(self):
        assert _sanitize_bot_name("Kiro " + _SPARKLES) == "Kiro"
        assert _sanitize_bot_name(_SPARKLES + " Kiro") == "Kiro"
        # An enclosing mark (the keycap) is an emoji by another route: it goes,
        # its base digit stays.
        assert _sanitize_bot_name("Kiro 1" + _VS16 + _KEYCAP) == "Kiro 1"

    def test_a_space_left_dangling_by_a_removal_is_folded(self):
        assert _sanitize_bot_name("Kiro " + _SPARKLES + " Bot") == "Kiro Bot"
        assert _sanitize_bot_name("A " + _SPARKLES + " " + _SPARKLES + " B") == "A B"
        # A double space the user typed is theirs to keep: nothing was removed.
        assert _sanitize_bot_name("Kiro  Bot") == "Kiro  Bot"

    def test_non_ascii_spaces_become_the_plain_space(self):
        # IDEOGRAPHIC SPACE, the space a Japanese keyboard types between words,
        # and NO-BREAK SPACE keep the words apart instead of vanishing.
        assert _sanitize_bot_name("\u30ad\u30ed\u3000\u30dc\u30c3\u30c8") == (
            "\u30ad\u30ed \u30dc\u30c3\u30c8"
        )
        assert _sanitize_bot_name("Kiro\u00a0Bot") == "Kiro Bot"

    def test_line_and_paragraph_separators_and_controls_are_dropped(self):
        assert _sanitize_bot_name("Kiro\u2028Bot") == "KiroBot"  # LINE SEPARATOR
        assert _sanitize_bot_name("Kiro\u2029Bot") == "KiroBot"  # PARAGRAPH SEPARATOR
        assert _sanitize_bot_name("Kiro\tBot\n") == "KiroBot"  # C0 controls
        assert _sanitize_bot_name("Kiro\x85Bot") == "KiroBot"  # NEXT LINE (C1 control)

    def test_private_use_and_surrogates_are_dropped(self):
        assert _sanitize_bot_name("Kiro\ue000") == "Kiro"  # a private-use character
        assert _sanitize_bot_name("Kiro\ud800") == "Kiro"  # a lone surrogate

    def test_compatibility_forms_are_kept_as_typed(self):
        # Full-width letters are letters by category. The tag rule refuses them
        # and names the plain spelling; a filter that drops cannot answer, and a
        # display name has no second-spelling problem to solve, so they stay.
        assert _sanitize_bot_name(_FULLWIDTH_KIRO) == _FULLWIDTH_KIRO

    def test_a_name_of_only_dropped_characters_is_empty(self):
        assert _sanitize_bot_name(_SPARKLES + " " + _SPARKLES) == ""

    def test_loader_keeps_a_korean_name_through_the_agent_section(self):
        """The issue's path: ``agent.bot_name`` read from config.json."""
        assert loader._build_agent_config({"bot_name": _KOREAN}).bot_name == _KOREAN
        assert loader._build_agent_config({"bot_name": _JOSE_NFD}).bot_name == _JOSE_NFC

    @pytest.mark.parametrize(
        "name",
        [
            _JOSE_NFC,
            _KOREAN,
            _JAPANESE,
            _CHINESE,
            _CYRILLIC,
            _HINDI,
            _THAI_WATER,
            _ARABIC_NAME,
            "\u5c0f\u3007",  # a letter number (Nl)
            "Kiro_2.0-b",  # the ASCII separators a tag shares
            "A\u0663",  # a non-ASCII decimal digit
        ],
    )
    def test_a_name_the_filter_keeps_whole_is_a_tag_the_tag_rule_accepts(self, name):
        """The two policies agree where a filter can agree with a validator.

        A tag has no space and opens with a letter or digit, so the sample stays
        inside that shape; the tag rule is the artifact store's, read here only
        as the reference the filter mirrors.
        """
        from kiro_crew.artifact_store.rules import normalize_tag

        assert _sanitize_bot_name(name) == name
        assert normalize_tag(name) == name


class TestBotNameSubstitution:
    def test_custom_name_substituted(self):
        from kiro_crew.context import ContextBuilder

        ctx = ContextBuilder(bot_name="Alita")
        assert ctx._substitute_bot_name("You are {bot_name} 🐾") == "You are Alita 🐾"

    def test_empty_defaults_from_config(self):
        from unittest.mock import patch

        from kiro_crew.context import ContextBuilder

        # When provider is ACP, default bot_name is "Kiro"
        with patch("kiro_crew.context.KiroCrewConfig.load") as mock_cfg:
            mock_cfg.return_value.agent.provider = "acp"
            ctx = ContextBuilder(bot_name="")
            assert ctx._substitute_bot_name("You are {bot_name}.") == "You are Kiro."

        # When provider is claude_code, default bot_name is "KiroCrew"
        with patch("kiro_crew.context.KiroCrewConfig.load") as mock_cfg:
            mock_cfg.return_value.agent.provider = "claude_code"
            ctx = ContextBuilder(bot_name="")
            assert ctx._substitute_bot_name("You are {bot_name}.") == "You are KiroCrew."

    def test_no_placeholder_is_noop(self):
        from kiro_crew.context import ContextBuilder

        ctx = ContextBuilder(bot_name="Alita")
        assert ctx._substitute_bot_name("No placeholder here.") == "No placeholder here."

    def test_self_referential_no_recursion(self):
        """bot_name containing {bot_name} — braces stripped by sanitizer."""
        from kiro_crew.context import ContextBuilder

        name = _sanitize_bot_name("{bot_name}")
        ctx = ContextBuilder(bot_name=name)
        assert ctx._substitute_bot_name("You are {bot_name}.") == "You are bot_name."
