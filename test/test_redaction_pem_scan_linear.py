"""A text that repeats a PEM header costs the credential scan linear time, and it finds every match ``search`` finds.

The PEM branch of ``_CREDENTIAL_PATTERNS`` is a header followed by a full block (the body,
lazily, up to the first END marker) or a truncated tail. A header that no END marker follows
fails the full block only after reading to the end of the text, and a text that repeats the
header does that once per header: quadratic, with the GIL held. Every page the dashboard
serves is redacted first, and a page can be 3 MiB. ``_credential_search`` asks for the text's
last END marker once and matches a header that no marker follows without the full-block tail.
These tests pin both halves: the matches are the plain ``search`` loop's, and such a header
never reaches the full-block tail.
"""

from __future__ import annotations

import random
import re
from typing import Any

import pytest

from kiro_crew.security import pem_scan, redaction

# The BEGIN markers are assembled from their dashes, so no line of this file holds a whole
# private-key header for a secret scanner to read.
_D = "-----"
_B = f"{_D}BEGIN PRIVATE KEY{_D}"
_BR = f"{_D}BEGIN RSA PRIVATE KEY{_D}"
_E = "-----END PRIVATE KEY-----"
_ER = "-----END RSA PRIVATE KEY-----"
_BODY = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC"
_AWS = "AKIAIOSFODNN7EXAMPLE"
_TAG = "[REDACTED: credential]"


def _search_loop(text: str) -> list[tuple[int, int]]:
    """The reference scan: one ``search`` per step from the last match."""
    spans, pos = [], 0
    while (m := redaction._CREDENTIAL_PATTERNS.search(text, pos)) is not None:
        spans.append(m.span())
        pos = max(m.end(), m.start() + 1)
    return spans


def _scan(text: str) -> list[tuple[int, int]]:
    return [m.span() for m in redaction._credential_matches(text)]


# (text, spans, redacted text), captured from the scan as it stood before the full-block
# tail was left out for a header with no END marker after it.
SHAPES: dict[str, tuple[str, list[tuple[int, int]], str]] = {
    "full rsa block": (f"{_BR}\n{_BODY}\n{_ER}", [(0, 94)], _TAG),
    "full block in prose": (
        f"key:\n{_B}\n{_BODY}\n{_E}\nthanks",
        [(5, 91)],
        f"key:\n{_TAG}\nthanks",
    ),
    "encrypted block with a blank line": (
        f"{_BR}\nProc-Type: 4,ENCRYPTED\nDEK-Info: AES-128-CBC,0123456789ABCDEF\n\n{_BODY}\n{_ER}",
        [(0, 157)],
        _TAG,
    ),
    "unterminated header": (_B, [(0, 27)], _TAG),
    "unterminated header, body lines": (f"{_B}\n{_BODY}\n{_BODY}\n", [(0, 93)], f"{_TAG}\n"),
    "unterminated header in prose": (
        f"see {_B} in the docs.\nnext line",
        [(4, 31)],
        f"see {_TAG} in the docs.\nnext line",
    ),
    "unterminated headers, LF": (
        (_B + "\n") * 3,
        [(0, 27), (28, 55), (56, 83)],
        f"{_TAG}\n{_TAG}\n{_TAG}\n",
    ),
    "unterminated headers, glued": (_B * 3, [(0, 27), (27, 54), (54, 81)], _TAG * 3),
    "nested begin": (f"{_B}\n{_BODY}\n{_BR}\n{_BODY}\n{_E}", [(0, 151)], _TAG),
    "two blocks": (
        f"{_B}\n{_BODY}\n{_E}\nmiddle\n{_BR}\n{_BODY}\n{_ER}\n",
        [(0, 86), (94, 188)],
        f"{_TAG}\nmiddle\n{_TAG}\n",
    ),
    "block, then an unterminated header": (
        f"{_B}\n{_BODY}\n{_E}\n{_B}\n{_BODY}\n",
        [(0, 86), (87, 147)],
        f"{_TAG}\n{_TAG}\n",
    ),
    "unterminated header, then a block": (
        f"{_B}\n{_BODY}\n\n\n{_BR}\n{_BODY}\n{_ER}",
        [(0, 157)],
        _TAG,
    ),
    "end before begin": (f"{_E}\n{_B}\n{_BODY}", [(26, 86)], f"{_E}\n{_TAG}"),
    "end without begin": (f"{_E}\nplain text", [], f"{_E}\nplain text"),
    "public key markers": (
        "-----BEGIN PUBLIC KEY-----\nMFkw\n-----END PUBLIC KEY-----",
        [],
        "-----BEGIN PUBLIC KEY-----\nMFkw\n-----END PUBLIC KEY-----",
    ),
    "public begin, private end": (
        f"-----BEGIN PUBLIC KEY-----\nMFkw\n{_E}",
        [],
        f"-----BEGIN PUBLIC KEY-----\nMFkw\n{_E}",
    ),
    "CRLF block": (f"{_B}\r\n{_BODY}\r\n{_E}\r\n", [(0, 88)], f"{_TAG}\r\n"),
    "CRLF unterminated": (f"{_B}\r\n{_BODY}\r\n", [(0, 61)], f"{_TAG}\r\n"),
    "CR-only block": (f"{_B}\r{_BODY}\r{_E}", [(0, 86)], _TAG),
    "blank lines, unterminated": (
        f"{_B}\n\n{_BODY}\n\n\n{_BODY}",
        [(0, 61)],
        f"{_TAG}\n\n\n{_BODY}",
    ),
    "overlapping end markers": (
        f"{_B}\n{_BODY}\n-----END PRIVATE KEY-----END PRIVATE KEY-----",
        [(0, 86)],
        f"{_TAG}END PRIVATE KEY-----",
    ),
    "end glued to the header": (f"{_B}{_E}", [(0, 52)], _TAG),
    "header at the very end": (f"text {_B}", [(5, 32)], f"text {_TAG}"),
    "header after the last end": (
        f"{_B}\n{_E}\n{_B}\n{_BODY}\ntail {_AWS}",
        [(0, 53), (54, 114), (120, 140)],
        f"{_TAG}\n{_TAG}\ntail {_TAG}",
    ),
    "other credential in the block": (f"{_B}\n{_AWS}\n{_E}", [(0, 74)], _TAG),
    "other credential after an unterminated header": (
        f"{_B}\n{_BODY}\nkey {_AWS}",
        [(0, 60), (65, 85)],
        f"{_TAG}\nkey {_TAG}",
    ),
}


@pytest.mark.parametrize("name", SHAPES)
def test_every_pem_shape_matches_and_redacts_as_before(name: str) -> None:
    text, spans, redacted = SHAPES[name]
    assert _scan(text) == spans
    assert _search_loop(text) == spans
    assert redaction.redact_credentials(text)[0] == redacted


_PIECES = (
    _B, _BR, f"{_D}BEGIN EC PRIVATE KEY{_D}", "-----BEGIN PUBLIC KEY-----",
    _E, _ER, "-----END PUBLIC KEY-----", "-----END ", "-----BEGIN ", "PRIVATE KEY-----", "-----",
    "\n", "\n", "\r\n", "\r", "\n\n", " ", "", _BODY, _BODY + "\n", "Proc-Type: 4,ENCRYPTED\n",
    "DEK-Info: AES-128-CBC,0123456789ABCDEF\n", "note: ", "text", _AWS, "ghp_" + "x" * 36,
)  # fmt: skip


def test_matches_equal_the_plain_search_loop_on_random_pem_text() -> None:
    rng = random.Random(20634)
    for _ in range(30_000):
        text = "".join(rng.choice(_PIECES) for _ in range(rng.randint(0, 24)))
        assert _scan(text) == _search_loop(text), text


_END_MARKER = re.compile(pem_scan.END)


def _brute_last_end(text: str) -> int:
    last = -1
    for i in range(len(text)):
        if _END_MARKER.match(text, i):
            last = i
    return last


_END_PIECES = (
    "-----END PRIVATE KEY-----", "-----END RSA PRIVATE KEY-----", "-----END PUBLIC KEY-----",
    "-----END ", "END PRIVATE KEY-----", "PRIVATE KEY-----", "-----", "-----ENDING PRIVATE KEY-----",
    "-----END privacy-----", "A", " ", "\n", "-",
)  # fmt: skip


def test_the_last_end_marker_is_the_one_a_scan_of_every_offset_finds() -> None:
    rng = random.Random(2063)
    for _ in range(8_000):
        text = "".join(rng.choice(_END_PIECES) for _ in range(rng.randint(0, 14)))
        assert pem_scan.last_end_start(text) == _brute_last_end(text), text


def test_the_derived_patterns_drop_exactly_the_full_block_tail() -> None:
    main = redaction._CREDENTIAL_PATTERNS.pattern
    tail = pem_scan.FULL_BLOCK + "|"
    assert main.count("-----END") == 1
    assert main.count(tail) == 1
    assert redaction._CREDENTIAL_PATTERNS_NO_PEM_BLOCK.pattern == main.replace(tail, "")
    assert "-----END" not in redaction._CREDENTIAL_PATTERNS_NO_PEM_BLOCK.pattern
    assert "-----END" not in redaction._CREDENTIAL_STARTS_SANS_JWT.pattern


class _CountingPattern:
    """A compiled pattern that counts the ``match`` calls made through it."""

    def __init__(self, real: re.Pattern[str]) -> None:
        self._real = real
        self.matches = 0

    def match(self, text: str, pos: int = 0) -> re.Match[str] | None:
        self.matches += 1
        return self._real.match(text, pos)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


@pytest.fixture
def counters(monkeypatch: pytest.MonkeyPatch) -> tuple[_CountingPattern, _CountingPattern]:
    full = _CountingPattern(redaction._CREDENTIAL_PATTERNS)
    bare = _CountingPattern(redaction._CREDENTIAL_PATTERNS_NO_PEM_BLOCK)
    monkeypatch.setattr(redaction, "_CREDENTIAL_PATTERNS", full)
    monkeypatch.setattr(redaction, "_CREDENTIAL_PATTERNS_NO_PEM_BLOCK", bare)
    return full, bare


@pytest.mark.parametrize("headers", [500, 2_000, 8_000])
def test_a_header_no_end_marker_follows_never_reaches_the_full_block_tail(
    counters: tuple[_CountingPattern, _CountingPattern], headers: int
) -> None:
    """The count does not grow with the text: no header reaches the pattern that reads on
    to an END marker, and each is matched once by the one without that tail."""
    full, bare = counters
    assert len(_scan((_B + "\n") * headers)) == headers
    assert (full.matches, bare.matches) == (0, headers)


def test_an_end_marker_before_every_header_is_the_same_case(
    counters: tuple[_CountingPattern, _CountingPattern],
) -> None:
    full, bare = counters
    assert len(_scan(_E + "\n" + (_B + "\n") * 1_000)) == 1_000
    assert (full.matches, bare.matches) == (0, 1_000)


def test_a_header_with_an_end_marker_after_it_still_reads_on_to_the_marker(
    counters: tuple[_CountingPattern, _CountingPattern],
) -> None:
    """The first header's block reaches the marker and takes every later header with it,
    so the full pattern runs once and the bare one never."""
    full, bare = counters
    text = (_B + "\n") * 1_000 + _E
    assert _scan(text) == [(0, len(text))]
    assert (full.matches, bare.matches) == (1, 0)


@pytest.mark.timeout(60)
def test_a_text_of_repeated_headers_at_the_dashboard_page_cap_is_redacted() -> None:
    """The case the cap raise exposed: 3 MiB of unterminated headers, which the quadratic
    scan did not finish, redacted whole by the public entry point."""
    text = (_B + "\n") * (3 * 1024 * 1024 // (len(_B) + 1))
    redacted, warnings = redaction.redact_credentials(text)
    assert "PRIVATE KEY" not in redacted
    assert len(warnings) == text.count(_B)
