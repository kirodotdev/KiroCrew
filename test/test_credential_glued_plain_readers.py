"""The glued scan's plain-code readers agree with the regex spellings they replace.

Each reference pattern below is a spelling the glued scan used before its readers
were written as plain code; it lives only here, as the oracle. Fixtures are random
joins of the pieces those spellings care about, never real values.
"""

from __future__ import annotations

import random
import re
import sys

from kiro_crew.security import pem_scan
from kiro_crew.security import redaction_patterns as rp

_SEP = r"[\"']?\s*[:=]\s*[\"']?"
_AWS_NAMES = (
    r"(?:SecretAccessKey|aws_secret_access_key|SessionToken|aws_session_token"
    r"|AccessKeyId|aws_access_key_id)"
)
_AWS_LABEL = re.compile(_AWS_NAMES + _SEP)
_BEARER = re.compile(r"(?i:Authorization)" + _SEP + r"(?i:Bearer)\s+")
_SPACED = re.compile(r"-----BEGIN |(?i:Authorization)|" + _AWS_NAMES[3:-1])
_URI = re.compile(
    r"(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis(?:s)?|amqp(?:s)?|https?|ftps?)://"
)
_PEM_END = re.compile(pem_scan.END)
_AWS_VALUE = re.compile(r"[^\s\"',}]*")
_AUTHORITY_END = re.compile(r"[\s/]")
_REST = re.compile(r"\S*")
_GLUED = re.compile(
    rp._CREDENTIAL_PATTERNS.pattern.replace(
        r"|(?<![A-Za-z0-9_-])[MNO][A-Za-z0-9_-]{22,30}", r"|[MNO][A-Za-z0-9_-]{22,30}", 1
    ).replace(r"|(?<![A-Za-z0-9_-])(?!(?<=\.)", r"|(?!(?<=\.)", 1)
)
_GLUED_SANS_JWT = re.compile(_GLUED.pattern.replace(f"|{rp.JWT_MULTI_SEGMENT}", "", 1))

_PIECES = (
    "Authorization",
    "AUTHORIZATION",
    "authorızation",
    "AUTHORİZATİON",
    "Authorizatio",
    "Bearer",
    "BEARER",
    "bEaReR",
    "Beare",
    ":",
    "=",
    '"',
    "'",
    " ",
    "\t",
    "\u3000",
    "\n",
    "SecretAccessKey",
    "aws_secret_access_key",
    "SessionToken",
    "aws_session_token",
    "AccessKeyId",
    "aws_access_key_id",
    "-----BEGIN ",
    "-----END ",
    "RSA ",
    "PRIVATE KEY",
    "PRIVATE KE",
    "-----",
    "-",
    "https://",
    "http://",
    "postgresql://",
    "postgres:/",
    "mongodb+srv://",
    "rediss://",
    "ftp",
    "://",
    "/",
    ",",
    "}",
    "x",
    "A",
    "_",
    "eyJ",
    ".",
    "M",
    "N",
    "O",
    "M" + "a" * 22,
    "b" * 6,
    "c" * 25,
    "eyJ" + "a" * 100,
    "d" * 43,
    "eyJhbGciOiJIUzI1NiJ9",
    "ghp_" + "e" * 36,
    "sk-ant-" + "f" * 16,
    "user:pw@",
    "N" + "a" * 23 + "." + "b" * 6 + "." + "c" * 27,
    "eyJ" + "a" * 97 + "." + "d" * 43,
)


def _texts(count: int) -> list[str]:
    rng = random.Random(23326)
    return ["".join(rng.choice(_PIECES) for _ in range(rng.randint(1, 12))) for _ in range(count)]


def _end(m: re.Match[str] | None) -> int:
    return -1 if m is None else m.end()


def _span(m: object) -> tuple[int, int, int] | None:
    if m is None:
        return None
    return (m.start(), m.end(), m.end("bearer_prefix"))  # type: ignore[attr-defined]


def test_the_label_scheme_and_marker_readers_agree_with_their_regex_spellings() -> None:
    for text in _texts(1500):
        for i in range(len(text) + 1):
            assert rp._aws_label_end(text, i) == _end(_AWS_LABEL.match(text, i)), (text, i)
            assert rp._bearer_prefix_end(text, i) == _end(_BEARER.match(text, i)), (text, i)
            assert rp._uri_scheme_end(text, i) == _end(_URI.match(text, i)), (text, i)
            assert rp._pem_end_at(text, i) == _end(_PEM_END.match(text, i)), (text, i)
            assert rp._run_end(text, i, rp._AWS_VALUE_STOPS) == _end(_AWS_VALUE.match(text, i))
            assert rp._run_end(text, i) == _end(_REST.match(text, i))
            boundary = _AUTHORITY_END.search(text, i)
            assert rp._run_end(text, i, "/") == (
                len(text) if boundary is None else boundary.start()
            )


def test_the_start_searches_agree_with_their_regex_spellings() -> None:
    rng = random.Random(6673)
    for text in _texts(1500):
        for _ in range(6):
            pos = rng.randint(0, len(text))
            endpos = rng.randint(pos, len(text))
            spaced = _SPACED.search(text, pos, endpos)
            assert rp._find_spaced_start(text, pos, endpos) == (
                -1 if spaced is None else spaced.start()
            )
            uri = _URI.search(text, pos, endpos)
            assert rp._find_uri_start(text, pos, endpos) == (-1 if uri is None else uri.start())
            marker = _PEM_END.search(text, pos)
            assert rp._find_pem_end(text, pos) == ((-1, -1) if marker is None else marker.span())


def test_a_glued_match_agrees_with_the_patterns_without_the_start_guards() -> None:
    rng = random.Random(5883)
    sliced = discord = 0
    for text in _texts(800):
        for i in range(len(text) + 1):
            endpos = rng.randint(i, len(text))
            for ours, oracle in (
                (rp._CREDENTIAL_PATTERNS, _GLUED),
                (rp._CREDENTIAL_PATTERNS_SANS_JWT, _GLUED_SANS_JWT),
            ):
                got = rp._glued_match(ours, text, i, endpos)
                assert _span(got) == _span(oracle.match(text, i, endpos)), (text, i, endpos)
                assert got is None or got.group() == text[got.start() : got.end()]
                sliced += isinstance(got, rp._Span)
            before = rng.randint(i, len(text) + 1)
            expected = next(
                (
                    s
                    for s in range(i, min(before, len(text)))
                    if text[s] in "MNO" and _GLUED.match(text, s, endpos)
                ),
                -1,
            )
            assert rp._find_glued_discord_start(text, i, before, endpos) == expected, (text, i)
            discord += expected != -1
    # The corpus reaches the guarded starts the slice exists for, and finds Discord ones.
    assert sliced and discord


def test_the_bearer_scheme_fold_agrees_with_the_regex_engine() -> None:
    """``str.lower`` and ``re.IGNORECASE`` fold the same code points onto each letter
    of ``Bearer``, so comparing it lowered matches what ``(?i:Bearer)`` matches."""
    every = [chr(cp) for cp in range(sys.maxunicode + 1)]
    for letter in "bear":
        engine = re.compile(f"(?i:{letter})")
        assert {c for c in every if engine.fullmatch(c)} == {
            c for c in every if c.lower() == letter
        }
