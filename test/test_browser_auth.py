"""Tests for the Netscape cookie-jar parser (kiro_crew.browser.auth)."""

from __future__ import annotations

from pathlib import Path

from kiro_crew.browser.auth import parse_netscape_cookies

# A tab-separated Netscape cookie line: domain, incl_subdomains, path,
# secure, expires, name, value.
_GOOD = "example.com\tTRUE\t/\tFALSE\t1999999999\tsid\tabc123"


def test_parses_a_valid_cookie(tmp_path: Path):
    jar = tmp_path / "cookies.txt"
    jar.write_text(_GOOD + "\n")
    cookies = parse_netscape_cookies(jar)
    assert len(cookies) == 1
    assert cookies[0]["name"] == "sid"
    assert cookies[0]["value"] == "abc123"
    assert cookies[0]["expires"] == 1999999999


def test_non_utf8_byte_does_not_abort_the_whole_jar(tmp_path: Path):
    """A single non-UTF-8 byte anywhere in the jar previously raised
    UnicodeDecodeError from path.read_text(encoding="utf-8") and dropped EVERY cookie. It must
    now degrade to U+FFFD on that line and still parse the good cookies."""
    jar = tmp_path / "cookies.txt"
    # A valid line, then a line with a raw 0xFF byte, then another valid line.
    good2 = "example.org\tTRUE\t/\tFALSE\t1999999999\ttok\txyz"
    jar.write_bytes(
        (_GOOD + "\n").encode("utf-8")
        + b"example.net\tTRUE\t/\tFALSE\t1999999999\tbad\t\xffval\n"
        + (good2 + "\n").encode("utf-8")
    )
    cookies = parse_netscape_cookies(jar)
    names = {c["name"] for c in cookies}
    # Both clean cookies survive; the mangled line is still parsed (not fatal).
    assert "sid" in names
    assert "tok" in names
    assert len(cookies) == 3


def test_unicode_digit_expiry_degrades_to_session_cookie(tmp_path: Path):
    """str.isdigit() is True for unicode digits ("²", "٣") that int() rejects,
    so the old gate still raised ValueError. A malformed expiry must degrade to
    a session cookie (-1), not crash the parser."""
    jar = tmp_path / "cookies.txt"
    jar.write_text(
        "example.com\tTRUE\t/\tFALSE\t²\tsid\tabc\n"  # superscript-2 expiry
        "example.com\tTRUE\t/\tFALSE\tnotanum\ttok\txyz\n"
    )
    cookies = parse_netscape_cookies(jar)
    assert len(cookies) == 2
    assert all(c["expires"] == -1 for c in cookies)


def test_negative_and_zero_expiry_become_session(tmp_path: Path):
    jar = tmp_path / "cookies.txt"
    jar.write_text(
        "example.com\tTRUE\t/\tFALSE\t-5\tsid\tabc\n"
        "example.com\tTRUE\t/\tFALSE\t0\ttok\txyz\n"
    )
    cookies = parse_netscape_cookies(jar)
    assert all(c["expires"] == -1 for c in cookies)
