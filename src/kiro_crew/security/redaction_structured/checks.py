"""Format checks a row names by id, and the placeholder / allow checks (REQ-F3)."""

from __future__ import annotations

import base64
import binascii
import json
import zlib
from typing import Any

from .charsets import ALPHABETS

_BASE62 = ALPHABETS["base62"]
_GITHUB_PREFIX_LEN = 4
_GITHUB_BODY_LEN = 30
_GITHUB_CHECK_LEN = 6
_DECODE_CAP = 16384


def base62_crc32(body: str) -> str:
    """The six-character base62 CRC32 a GitHub token appends to its body."""
    crc = zlib.crc32(body.encode("ascii"))
    digits = ""
    while crc:
        crc, rem = divmod(crc, 62)
        digits = _BASE62[rem] + digits
    return digits.rjust(_GITHUB_CHECK_LEN, "0")


def github_crc32_ok(token: str) -> bool:
    body = token[_GITHUB_PREFIX_LEN : _GITHUB_PREFIX_LEN + _GITHUB_BODY_LEN]
    check = token[_GITHUB_PREFIX_LEN + _GITHUB_BODY_LEN :]
    if len(body) != _GITHUB_BODY_LEN or len(check) != _GITHUB_CHECK_LEN:
        return False
    return base62_crc32(body) == check


CHECKSUMS = {"github_crc32": github_crc32_ok}


def b64url_json_object(segment: str) -> dict[str, Any] | None:
    """Decode a base64url segment (size-capped) to a JSON object, else ``None``."""
    if len(segment) > _DECODE_CAP:
        return None
    try:
        raw = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
        decoded = json.loads(raw.decode("utf-8"))
    except (binascii.Error, ValueError, RecursionError):
        return None
    return decoded if isinstance(decoded, dict) else None


# ── Placeholders and allow entries ──

ALLOW_WORDS = frozenset(
    {
        "changeme",
        "placeholder",
        "example",
        "true",
        "false",
        "null",
        "none",
        "required",
        "optional",
        "redacted",
        "unset",
        "empty",
        "string",
        "secret",
        "password",
    }
)
#: Words a documented example writes where the value goes (``sk_test_YOUR_KEY_HERE``,
#: ``Bearer your-token-here``), read as whole parts of the value.
PLACEHOLDER_PARTS = frozenset({"your", "here", "placeholder", "changeme"})
#: The same for a token-format body after its prefix (``xoxb-slack-bot-token``).
BODY_PLACEHOLDER_PARTS = PLACEHOLDER_PARTS | {"token"}
#: A run of one mask character this long marks an elided value, and a ``your`` with one
#: of these words after it a written-out one, even where a renderer dropped the
#: separators (``ghpxxxxxxxx``, ``sktestYOURKEYHERE``).
MASK_RUN_MIN = 8
_YOUR_TAILS = ("here", "key", "token", "secret", "password")
_EXAMPLE_SUFFIXES = ("EXAMPLEKEY", "EXAMPLE")
_MASK_CHARS = frozenset("*xX.\u2026")
_ENV_HEAD = frozenset(ALPHABETS["upper_digit"].replace("0123456789", "") + "_")
_ENV_TAIL = frozenset(ALPHABETS["upper_digit"] + "_")
_IDENT = frozenset(ALPHABETS["b64url"])
_REF_HEAD = frozenset(ALPHABETS["base62"].replace("0123456789", "") + "_")
_REF_TAIL = frozenset(ALPHABETS["base62"] + "_")
# Where a value is cut into parts: ``-``, ``_``, ``.`` and a blank.
_PART_BREAKS = str.maketrans("-_.", "   ")
_LOWER = frozenset("abcdefghijklmnopqrstuvwxyz")
_WORD_CHARS = _LOWER | frozenset("-_. ")
_VOWELS = frozenset("aeiouy")
#: Longer than most words a config or a document writes; a lower-case part longer than
#: this reads as generated (a 16-letter app password).
WORD_MAX = 14


def _braced(value: str) -> bool:
    if value.startswith("${") and value.endswith("}") and len(value) > 3:
        inner = value[2:-1]
        return "{" not in inner and "}" not in inner and "\n" not in inner
    body = value[1:] if value.startswith("$") else value
    if body.startswith("{{") and body.endswith("}}") and len(body) > 4:
        inner = body[2:-2]
        return "}}" not in inner and "\n" not in inner
    return False


def is_placeholder(value: str) -> bool:
    """A documented example value or placeholder, recognised without any pattern."""
    v = value.strip()
    if len(v) < 4:
        return True
    folded = v.casefold()
    if folded in ALLOW_WORDS:
        return True
    if v.endswith(_EXAMPLE_SUFFIXES):
        return True
    if folded.startswith(("your-", "your_")):
        return True
    if v[0] == "<" and v[-1] == ">" and not (set(v[1:-1]) & {"<", ">", "\n", "\r"}):
        return True
    if _braced(v):
        return True
    if v[0] == "$" and len(v) > 1 and v[1] in _ENV_HEAD and set(v[2:]) <= _ENV_TAIL:
        return True
    # A shell or template variable of any case (``"$password"``).
    if v[0] == "$" and len(v) > 1 and v[1] in _REF_HEAD and set(v[2:]) <= _REF_TAIL:
        return True
    if v[0] == "%" == v[-1] and len(v) > 2 and set(v[1:-1]) <= _IDENT:
        return True
    if len(v) >= 3 and len(set(v)) == 1 and v[0] in _MASK_CHARS:
        return True
    if any(ch * MASK_RUN_MIN in folded for ch in "x*\u2026"):
        return True
    your = folded.find("your")
    if your != -1 and any(tail in folded[your + 4 :] for tail in _YOUR_TAILS):
        return True
    return _has_placeholder_part(folded, PLACEHOLDER_PARTS)


def _has_placeholder_part(folded: str, words: frozenset[str]) -> bool:
    """Whether a part of *folded* is a placeholder word or a mask run (``ghp_xxxx``)."""
    for part in folded.translate(_PART_BREAKS).split():
        if part in words or (len(part) >= 3 and len(set(part)) == 1 and part[0] in _MASK_CHARS):
            return True
    return False


def is_placeholder_body(body: str) -> bool:
    """A token-format body a documented example writes (``xoxb-your-bot-token``)."""
    return is_placeholder(body) or _has_placeholder_part(body.casefold(), BODY_PLACEHOLDER_PARTS)


def reads_as_words(value: str) -> bool:
    """Lower-case words with no digit (``my-secret-pw``, ``kerberos``, ``see the vault``).

    Prose or a documented example, not a generated value: a part longer than
    :data:`WORD_MAX`, or a part of four letters or more with no vowel, reads as generated.
    """
    if not set(value) <= _WORD_CHARS:
        return False
    for part in value.translate(_PART_BREAKS).split():
        if len(part) > WORD_MAX or (len(part) > 3 and _VOWELS.isdisjoint(part)):
            return False
    return True


#: Words a documented example or a sentence writes in a value (``my-secret-pw``).
EXAMPLE_WORDS = frozenset(
    {"secret", "password", "passwd", "pass", "pw", "pwd", "example", "sample", "dummy"}
    | {"test", "demo", "changeme", "your", "here", "value", "token", "key", "default"}
)


def reads_as_example_words(value: str) -> bool:
    """A labelled value that is a sentence or a documented example, not a password.

    Lower-case words (:func:`reads_as_words`) that are prose (a blank between them) or
    that name what the value stands for (``my-secret-pw``). One word alone, or words
    with no such part (``correct-horse-battery``), can be a real password and is not
    this.
    """
    if not reads_as_words(value):
        return False
    parts = value.translate(_PART_BREAKS).split()
    if len(parts) < 2:
        return False
    return " " in value.strip() or any(part in EXAMPLE_WORDS for part in parts)
