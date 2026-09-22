"""Tests for the stdlib-only terminal line sanitizer."""

from kiro_crew.terminal_safe import normalize_for_scanning, safe_terminal_line


def test_removes_osc_broad_csi_two_byte_escape_and_controls() -> None:
    value = "a\x1b]0;hidden\x07b" "\x1b[>1;2mc" "\x1bMd" "\x00e\x7ff\x9fg"

    assert safe_terminal_line(value) == "abcdefg"


def test_keeps_a_forged_result_line_on_the_prefixed_line() -> None:
    rendered = safe_terminal_line("ok\n✅ enabled evil")

    assert "\n" not in rendered
    assert rendered == "ok\\x0a✅ enabled evil"


def test_strips_carriage_return_and_controls() -> None:
    assert safe_terminal_line("a\rb\x1b[31mc") == "abc"


def test_preserves_tabs_and_ordinary_unicode() -> None:
    assert safe_terminal_line("one\ttwø") == "one\ttwø"


def test_caps_with_an_ellipsis() -> None:
    rendered = safe_terminal_line("x" * 3000)

    assert len(rendered) == 2000
    assert rendered.endswith("…")
    assert safe_terminal_line("x" * 2000) == "x" * 2000


def test_truncates_after_escaping() -> None:
    rendered = safe_terminal_line("\n" * 1500)

    assert len(rendered) == 2000
    assert rendered.endswith("…")
    assert "\n" not in rendered


def test_normalize_removes_control_characters_and_keeps_the_rest() -> None:
    """Control characters go; every printable byte, including a payload, stays."""
    value = "a\x1b]0;hidden\x07b" "\x1b[>1;2mc" "\x1bMd" "\x00e\x7ff\x9fg"

    assert normalize_for_scanning(value) == "a]0;hiddenb[>1;2mcMdefg"


def test_normalize_joins_a_token_split_by_control_characters() -> None:
    assert normalize_for_scanning("AKIA\x1b\x9bSUFFIX") == "AKIASUFFIX"


def test_normalize_keeps_tabs_newlines_and_carriage_returns() -> None:
    """Dropping the carriage return would rewrite every CRLF document into LF."""
    assert normalize_for_scanning("one\ttwo\nthree") == "one\ttwo\nthree"
    assert normalize_for_scanning("one\r\ntwo") == "one\r\ntwo"


def test_normalize_never_deletes_visible_text() -> None:
    """Text between an introducer and a later terminator is content, so it is kept.

    Consuming a control string whole would delete however much visible text sits
    between the two, which loses a stored note rather than sanitising it.
    """
    assert normalize_for_scanning("keep \x9d this text \x9c too") == "keep  this text  too"


def test_normalize_leaves_line_confinement_to_the_renderer() -> None:
    """The scanner applies the control policy only; the newline rewrite is rendering's job."""
    assert "\\x0a" not in normalize_for_scanning("ok\nnext")


def test_the_renderer_consumes_what_the_scanner_keeps() -> None:
    """The two policies differ on a sequence's payload, and that difference is intended.

    A renderer reproduces the terminal, which acts on the whole sequence and shows none
    of it. A scanner keeps the printable bytes, because deleting them would remove a
    user's visible content from an API response.
    """
    assert safe_terminal_line("a\x1b[0mb") == "ab"
    assert normalize_for_scanning("a\x1b[0mb") == "a[0mb"


def test_normalize_removes_invisible_unicode_separators() -> None:
    """Zero-width and BIDI characters render as nothing, so removing them loses nothing.

    Left in, one of them splits a token while showing nothing, and any consumer that
    drops default-ignorable code points rejoins what the scanner failed to match.
    """
    assert normalize_for_scanning("AKIA\u200bSUFFIX") == "AKIASUFFIX"
    assert normalize_for_scanning("a\u200c\u200d\u2060b") == "ab"  # ASCII on both sides
    assert normalize_for_scanning("a\u00adb") == "ab"
    assert normalize_for_scanning("a\u202eb") == "ab"
    assert normalize_for_scanning("a\ufeffb") == "ab"
    assert normalize_for_scanning("a\U000e0041b") == "ab"


def test_normalize_keeps_visible_unicode() -> None:
    """Ordinary non-ASCII text is content and passes through untouched."""
    assert normalize_for_scanning("one twø tría Ωmega emoji 🙂") == "one twø tría Ωmega emoji 🙂"


def test_normalize_keeps_a_format_char_flanked_by_non_ascii() -> None:
    """A joiner doing typographic work is content, so it survives.

    U+200D joins the parts of an emoji sequence and U+200C shapes Persian text. No ASCII
    credential can span such a position, so keeping the character there hides nothing
    while removing it would change what the reader sees.
    """
    emoji = "\U0001f9d1\u200d\U0001f4bb"
    family = "\U0001f468\u200d\U0001f469\u200d\U0001f467"
    persian = "\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645"
    bidi = "\u05d0\u200f\u05d1"

    assert normalize_for_scanning(emoji) == emoji
    assert normalize_for_scanning(family) == family
    assert normalize_for_scanning(persian) == persian
    assert normalize_for_scanning(bidi) == bidi


def test_normalize_still_removes_a_format_char_an_ascii_token_could_straddle() -> None:
    """ASCII on BOTH sides is the case a token can straddle, and there the run goes.

    Either side being non-ASCII, or missing, means no ASCII token sits across the run, so
    removing it would buy nothing and could only damage real text.
    """
    assert normalize_for_scanning("AKIA\u200bSUFFIX") == "AKIASUFFIX"
    assert normalize_for_scanning("AKIA\u200dSUFFIX") == "AKIASUFFIX"
    assert normalize_for_scanning("\u200dabc") == "\u200dabc"
    assert normalize_for_scanning("abc\u200d") == "abc\u200d"
    assert normalize_for_scanning("\U0001f9d1\u200dabc") == "\U0001f9d1\u200dabc"
    assert normalize_for_scanning("abc\u200d\U0001f9d1") == "abc\u200d\U0001f9d1"


def test_normalize_removes_a_whole_run_of_format_chars() -> None:
    """The flanking test applies to the run, so a run's middle cannot survive.

    Format characters are themselves non-ASCII, so testing each one separately would
    find the middle of a run flanked on both sides and keep it. One invisible character
    left inside an ASCII token is the whole defect.
    """
    assert normalize_for_scanning("AKIA\u200b\u200b\u200bSUFFIX") == "AKIASUFFIX"
    assert normalize_for_scanning("a\u200c\u200d\u2060b") == "ab"
    assert normalize_for_scanning("\U0001f9d1\u200d\u200dabc") == "\U0001f9d1\u200d\u200dabc"


def test_a_control_char_is_not_a_load_bearing_flank() -> None:
    """Controls go in a first pass, so they cannot pose as non-ASCII neighbours.

    A control character is itself non-ASCII. Judged in one pass against the original
    text, the controls on either side of this joiner would read as a load-bearing flank
    and the joiner would survive inside an ASCII token, which is the whole defect.
    """
    assert normalize_for_scanning("AKIA\x9b\u200d\x9cSUFFIX") == "AKIASUFFIX"
    assert normalize_for_scanning("a\x00\u200b\x00b") == "ab"
    assert normalize_for_scanning("a\x1b\u200d\x1bb") == "ab"


def test_normalize_removes_invisibles_that_are_not_category_cf() -> None:
    """A category test alone walks past these, and each one splits a token unseen."""
    assert normalize_for_scanning("AKIA\ufe0fSUFFIX") == "AKIASUFFIX"
    assert normalize_for_scanning("AKIA\u034fSUFFIX") == "AKIASUFFIX"
    assert normalize_for_scanning("AKIA\u3164SUFFIX") == "AKIASUFFIX"
    assert normalize_for_scanning("AKIA\u180bSUFFIX") == "AKIASUFFIX"
    assert normalize_for_scanning("AKIA\U000e0100SUFFIX") == "AKIASUFFIX"
    assert normalize_for_scanning("AKIA\u17b4SUFFIX") == "AKIASUFFIX"


def test_normalize_removes_the_reserved_stretches_of_the_tag_block() -> None:
    """Plane 14's tag block is invisible end to end, reserved stretches included.

    Its language tag and tag characters are ``Cf`` and its variation selectors are ``Mn``,
    so a category test reaches those two. The reserved stretches between them are ``Cn``,
    which every category test walks straight past, and a renderer draws them exactly as it
    draws the rest of the block: as nothing. So each one splits a token unseen.
    """
    token = "AKIAIOSFODNN7EXAMPLE"

    for code in (0xE0000, 0xE0002, 0xE001F, 0xE0080, 0xE00FF, 0xE01F0, 0xE0FFF):
        split = token[:8] + chr(code) + token[8:]
        assert normalize_for_scanning(split) == token, f"U+{code:05X}"


def test_normalize_keeps_the_code_points_either_side_of_the_tag_block() -> None:
    """The block is a bounded range, so its neighbours stay content."""
    for code in (0xDFFFF, 0xE1000, 0xE1001):
        neighbour = chr(code)
        assert neighbour in normalize_for_scanning(f"a{neighbour}b"), f"U+{code:05X}"


def test_normalize_keeps_an_emoji_presentation_selector() -> None:
    """U+FE0F follows a non-ASCII base, so it is doing work and stays.

    The single test is on the character BEFORE the run: an ASCII token straddling it
    needs ASCII right there, and a non-ASCII base already breaks any such token.
    """
    heart = "\u2764\ufe0f"

    assert normalize_for_scanning(heart) == heart
    assert normalize_for_scanning(f"a {heart} b") == f"a {heart} b"


def test_normalize_keeps_an_ascii_keycap_sequence() -> None:
    """A keycap's base is ASCII, and dropping its selector rewrites the user's text.

    ``#``, ``*`` and each digit take U+FE0F then U+20E3 to spell a keycap. U+20E3 is an
    enclosing mark, so it is non-ASCII and the general rule keeps the selector between
    them without needing a case of its own. Removing the selector would leave the bare
    base wearing a keycap box, which is content the field owner never wrote, and an edit
    round-trip persists that.
    """
    assert normalize_for_scanning("Press 1\ufe0f\u20e3 now") == "Press 1\ufe0f\u20e3 now"

    for base in ["#", "*", *"0123456789"]:
        sequence = f"{base}\ufe0f\u20e3"
        assert normalize_for_scanning(f"x {sequence} y") == f"x {sequence} y"

    assert normalize_for_scanning("7\ufe0e\u20e3") == "7\ufe0e\u20e3"


def test_normalize_keeps_a_bidi_mark_between_a_digit_and_rtl_text() -> None:
    """A mark ordering mixed-direction text is content, and an edit round-trip keeps it.

    An LRM after a Latin digit, before Arabic, is what an ordinary user writes. Its left
    side is ASCII and its right side is not, so no ASCII token straddles it.
    """
    arabic = "\u0627\u0644\u0639\u0631\u0628\u064a\u0629"
    hebrew = "\u05e9\u05dc\u05d5\u05dd"

    assert normalize_for_scanning(f"1\u200e{arabic}") == f"1\u200e{arabic}"
    assert normalize_for_scanning(f"5\u200f{hebrew}") == f"5\u200f{hebrew}"
    assert normalize_for_scanning(f"{arabic}\u200e1") == f"{arabic}\u200e1"


def test_normalize_needs_ascii_on_both_sides_before_it_removes_a_run() -> None:
    """One side is not enough, and that holds whichever side is non-ASCII or absent."""
    assert normalize_for_scanning("a\ufe0fb") == "ab"
    assert normalize_for_scanning("1\ufe0fx\u20e3") == "1x\u20e3"

    assert normalize_for_scanning("\ufe0f\u20e3") == "\ufe0f\u20e3"
    assert normalize_for_scanning("1\ufe0f\u200b\u20e3") == "1\ufe0f\u200b\u20e3"
    assert normalize_for_scanning("1\u200b\u20e3") == "1\u200b\u20e3"


def test_a_run_kept_beside_non_ascii_cannot_hide_a_split_token() -> None:
    """The kept cases are exactly the ones no ASCII token can sit across."""
    token = "ghp_" + "A" * 36

    assert normalize_for_scanning(token[:10] + "\ufe0f" + token[10:]) == token

    with_keycap = normalize_for_scanning(token[:10] + "\ufe0f\u20e3" + token[10:])
    assert token not in with_keycap
    assert "\u20e3" in with_keycap


#: Unicode's Default_Ignorable_Code_Point property, from DerivedCoreProperties. Written out
#: here instead of read from the module under test, so it is an independent statement of
#: what has to be invisible: Python exposes no property lookup to ask for it instead, which
#: is why an implementation has to enumerate, and why an enumeration can miss a member.
_DEFAULT_IGNORABLE = (
    (0x00AD, 0x00AD),
    (0x034F, 0x034F),
    (0x061C, 0x061C),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x206F),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0),
    (0xFFF0, 0xFFF8),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)


def test_normalize_treats_every_default_ignorable_code_point_as_invisible() -> None:
    """The whole property, not the members one enumeration happens to list.

    Each of these renders as nothing, so each can sit inside a token and hide it from a
    scanner that matches a pattern. Checking the property rather than a handful of samples
    is what catches a member left out, whatever category it carries.
    """
    token = "AKIAIOSFODNN7EXAMPLE"
    missed = [
        f"U+{code:04X}"
        for start, end in _DEFAULT_IGNORABLE
        for code in range(start, end + 1)
        if normalize_for_scanning(token[:8] + chr(code) + token[8:]) != token
    ]

    assert not missed, f"default-ignorable code points left in place: {missed[:20]}"
