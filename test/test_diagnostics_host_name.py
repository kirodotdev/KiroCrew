"""Every spelling of this machine's host name is replaced in a diagnostics bundle.

The bundle scrub (`diagnostics._scrub_bundle`) strips hidden characters, redacts
credentials, collapses home prefixes and then replaces the host name, so these
tests drive that whole pipeline on one member's text.
"""

from __future__ import annotations

import socket
import zipfile

import pytest
from test_diagnostics import _isolate

from kiro_crew import diagnostics
from kiro_crew.dashboard import crash_dump_store


def _host(monkeypatch, name: str) -> None:
    monkeypatch.setattr(socket, "gethostname", lambda: name)
    monkeypatch.setattr(crash_dump_store, "_pid_domain", lambda: f"{name}/pid:[1]")


def _scrubbed(text: str) -> str:
    return diagnostics._scrub_bundle(text)[0]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("boot\fbox ok", "boot<host> ok"),
        ("boot\x0bbox ok", "boot<host> ok"),
        ("boot\u200bbox ok", "boot<host> ok"),
        ("boot\f%62ox ok", "boot<host> ok"),
        ("boot\fbox\fbox ok", "boot<host> ok"),
        ("x\x1b[38;2;255;255;255;48;2;0;0;0mbox\x1b[0m", "x[38;2;255;255;255;48;2;0;0;0m<host>[0m"),
        (
            "x\\u001b[38;2;255;255;255;48;2;0;0;0mbox",
            "x\\u001b[38;2;255;255;255;48;2;0;0;0m<host>",
        ),
        ("GET /a%2Fbox HTTP", "GET /a%2F<host> HTTP"),
        ("%62ox", "<host>"),
        ("C:\\Users\\box\\logs", "C:\\Users\\<host>\\logs"),
        ("\\\\box\\share", "\\\\<host>\\share"),
        ('{"p":"C:\\\\Users\\\\BOX"}', '{"p":"C:\\\\Users\\\\<host>"}'),
    ],
    ids=[
        "form-feed",
        "vertical-tab",
        "zwsp",
        "joined-encoded",
        "joined-twice",
        "truecolor-raw",
        "truecolor-serialized",
        "percent-slash",
        "percent-letter",
        "windows-path",
        "unc-path",
        "json-escaped-path",
    ],
)
def test_a_short_host_name_is_replaced_in_every_spelling(monkeypatch, raw, expected):
    _host(monkeypatch, "box")
    assert _scrubbed(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "jane\\u002dlaptop",
        "jane\\u002Dlaptop",
        "jane\\x2dlaptop",
        "jane%2Dlaptop",
        "JANE%2dLAPTOP",
        "%6Aane-laptop",
        "\\u006a\\u0061ne-laptop",
    ],
)
def test_a_long_host_name_is_replaced_with_an_encoded_character(monkeypatch, raw):
    _host(monkeypatch, "jane-laptop")
    assert _scrubbed(f"on {raw} now") == "on <host> now"


@pytest.mark.parametrize(
    ("host", "raw"),
    [
        ("bücher-pc", "xn--bcher-pc-65a"),
        ("xn--bcher-pc-65a", "BÜCHER-PC"),
        ("XN--BCHER-PC-65A", "bücher-pc"),
        ("bücher-pc", "b%C3%BCcher-pc"),
        ("bücher-pc", "b%c3%bccher%2Dpc"),
        ("bücher-pc", "b\\xc3\\xbccher-pc"),
    ],
    ids=[
        "unicode-name-punycode-text",
        "punycode-name-unicode-text",
        "upper-case-punycode-name",
        "utf8-percent-encoded",
        "utf8-percent-lower-case-hex",
        "utf8-byte-escapes",
    ],
)
def test_the_other_idna_spelling_is_replaced(monkeypatch, host, raw):
    _host(monkeypatch, host)
    assert _scrubbed(f"host={raw};") == "host=<host>;"


def test_a_name_outside_the_basic_plane_is_replaced_from_a_surrogate_pair(monkeypatch):
    """`jane-😀-pc` written as JSON escapes the emoji as a surrogate pair."""
    _host(monkeypatch, "xn--jane--pc-gi75g")
    raw = '{"host":"jane-\\ud83d\\ude00-pc","alt":"JANE-\\uD83D\\uDE00-PC"}'
    assert _scrubbed(raw) == '{"host":"<host>","alt":"<host>"}'


def test_a_lone_surrogate_escape_is_left_alone(monkeypatch):
    _host(monkeypatch, "box")
    assert _scrubbed("\\ud83dbox \\ude00 box") == "\\ud83d<host> \\ude00 <host>"


def test_a_byte_that_starts_no_utf8_sequence_still_decodes(monkeypatch):
    """A stray or truncated byte reads as one character, and the run after it is
    still decoded."""
    _host(monkeypatch, "jane-laptop")
    assert _scrubbed("%FF%C3jane%2Dlaptop \\xe2\\x82") == "%FF%C3<host> \\xe2\\x82"


@pytest.mark.parametrize(
    ("host", "raw", "expected"),
    [
        ("iris", "boot\firis\nİRİS kapı\n", "boot<host>\n<host> kapı\n"),
        ("pi", "a\fpi\nkapı kapİ\n", "a<host>\nkapı kapİ\n"),
        ("nas", "x\fnas\nnaſ\n", "x<host>\n<host>\n"),
    ],
    ids=["dotted-capital-i", "dotless-i", "long-s"],
)
def test_a_unicode_case_variant_beside_a_join_does_not_abort_the_scrub(
    monkeypatch, host, raw, expected
):
    """`re.IGNORECASE` matches `İ`, `ı` and `ſ` against ASCII letters, and their
    folded forms are no recorded joined name, so the join lookup skips them. A
    variant that stands as a whole word is the name in lookalike letters and the
    ordinary pass replaces it."""
    _host(monkeypatch, host)
    assert _scrubbed(raw) == expected


def test_every_one_of_many_distinct_joins_is_replaced(monkeypatch):
    _host(monkeypatch, "box")
    raw = "".join(f"entry{i:08d}\fbox\n" for i in range(200))
    assert _scrubbed(raw) == "".join(f"entry{i:08d}<host>\n" for i in range(200))


def test_a_join_keeps_a_bounded_prefix_and_still_matches_after_a_long_word(monkeypatch):
    """Only the last `_JOIN_CONTEXT` characters of the word are kept, which bounds
    the lookup per occurrence."""
    from kiro_crew import diagnostics_host_name

    _host(monkeypatch, "box")
    word = "w" * 40
    raw = f"{word}\fbox ok"
    joined = diagnostics_host_name.find_joined_host_words(raw, diagnostics_host_name.host_names())
    assert {len(w.prefix) for w in joined} == {diagnostics_host_name._JOIN_CONTEXT}
    assert _scrubbed(raw) == f"{word}<host> ok"


def test_ordinary_words_and_other_machines_survive(monkeypatch):
    _host(monkeypatch, "box")
    text = "boxes inbox my-box vm_box 1box toolbox\n"
    assert _scrubbed(text) == text


@pytest.mark.parametrize(
    "text",
    [
        '{"msg":"done\\nAs expected"}',
        '{"msg":"tab\\tom said"}',
        "line\\nas text",
        '{"msg":"error:\\nAs expected"}',
        '{"p":"C:\\\\x","m":"done\\nAs expected"}',
    ],
    ids=["newline-escape", "tab-escape", "plain-text", "colon-before", "path-in-other-field"],
)
def test_a_serialized_escape_letter_is_not_taken_for_a_short_name(monkeypatch, text):
    """`\\n` + `As` reads like `\\nas`; outside a Windows path the escape stays whole."""
    _host(monkeypatch, "nas" if "nas" in text.lower() else "tom")
    assert _scrubbed(text) == text


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("C:\\Users\\nas", "C:\\Users\\<host>"),
        ("open C:\\Users\\nas now", "open C:\\Users\\<host> now"),
        ("\\\\nas\\share", "\\\\<host>\\share"),
        ("x\\nas\\y", "x\\<host>\\y"),
        ("D:\\Project Files\\nas", "D:\\Project Files\\<host>"),
        ("D:\\folder(x)\\nas", "D:\\folder(x)\\<host>"),
        ("\\\\srv\\a b;c=d\\nas", "\\\\srv\\a b;c=d\\<host>"),
        ("open ~\\Desktop\\nas.log", "open ~\\Desktop\\<host>.log"),
        ("~\\nas.log", "~\\<host>.log"),
        ("\\\\nas", "\\\\<host>"),
    ],
    ids=[
        "drive-path-end",
        "drive-path-in-text",
        "unc",
        "separator-after",
        "space-in-path",
        "paren-in-path",
        "unc-with-punctuation",
        "collapsed-home-subdir",
        "collapsed-home-root",
        "unc-end",
    ],
)
def test_a_short_name_starting_with_an_escape_letter_is_replaced_in_a_path(
    monkeypatch, raw, expected
):
    _host(monkeypatch, "nas")
    assert _scrubbed(raw) == expected


def test_a_one_letter_host_name_leaves_escapes_and_paths_intact(monkeypatch):
    _host(monkeypatch, "t")
    text = '{"stderr":"failure\\t","path":"C:\\\\tmp\\\\t"}'
    assert _scrubbed(text) == '{"stderr":"failure\\t","path":"C:\\\\tmp\\\\<host>"}'


def test_a_joined_host_name_keeps_a_credential_redacted(monkeypatch):
    """The join is read before the scrub, the replacement made after it."""
    _host(monkeypatch, "token")
    raw = "boot\ftoken\nAuthorization: Token abc-123-def-456-ghijklmno\n"
    clean = _scrubbed(raw)
    assert clean.startswith("boot<host>\n"), clean
    assert "abc-123-def-456-ghijklmno" not in clean, clean


def test_the_bundle_replaces_a_short_name_a_form_feed_joined_on(tmp_path, monkeypatch):
    """The issue's reproduction: `boot<FF>box` in gateway.log on a machine named box."""
    home = tmp_path / "home"
    _isolate(monkeypatch, home)
    _host(monkeypatch, "box")
    (home / "gateway.log").write_text("boot\fbox\n")

    r = diagnostics.collect_bundle(output_dir=tmp_path / "out")

    with zipfile.ZipFile(r.zip_path) as z:
        assert z.read("gateway.log").decode() == "boot<host>\n"
